# Discord module for PyLink
#
# Copyright (C) 2018-2019 Ian Carpenter <icarpenter@cultnet.net>
# Copyright (C) 2018-2019 James Lu <james@overdrivenetworks.com>
#
# This program is free software; you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation; either version 2 of the License, or
# (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License along
# with this program. If not, see <http://www.gnu.org/licenses/>.
import calendar
import time
import collections
import string
import urllib.parse

import socket, gevent.socket

if socket.socket is not gevent.socket.socket:
    raise ImportError("gevent patching must be enabled for protocols/discord to work!")

from disco.api.http import APIException
from disco.bot import Bot, BotConfig
from disco.bot import Plugin
from disco.client import Client, ClientConfig
from disco.gateway import events
from disco.types import Guild, Channel as DiscordChannel, GuildMember, Message
from disco.types.channel import ChannelType
from disco.types.permissions import Permissions
from disco.types.user import Status as DiscordStatus
#from disco.util.logging import setup_logging
from holster.emitter import Priority

from pylinkirc import structures, utils
from pylinkirc.classes import *
from pylinkirc.log import log
from pylinkirc.protocols.clientbot import ClientbotBaseProtocol

try:
    import libgravatar
except ImportError:
    libgravatar = None
    log.info('discord: libgravatar not installed - avatar support will be disabled.')

from ._discord_formatter import I2DFormatter, D2IFormatter

BATCH_DELAY = 0.3  # TODO: make this configurable

class DiscordChannelState(structures.CaseInsensitiveDict):
    @staticmethod
    def _keymangle(key):
        try:
            key = int(key)
        except (TypeError, ValueError):
            raise KeyError("Cannot convert channel ID %r to int" % key)
        return key

class DiscordBotPlugin(Plugin):
    # TODO: maybe this could be made configurable?
    # N.B. iteration order matters: we stop adding lower modes once someone has +o, much like
    #      real services
    irc_discord_perm_mapping = collections.OrderedDict(
        [('admin', Permissions.ADMINISTRATOR),
         ('op', Permissions.MANAGE_MESSAGES),
         ('halfop', Permissions.KICK_MEMBERS),
         ('voice', Permissions.SEND_MESSAGES),
        ])
    botuser = None
    status_mapping = {
        'ONLINE': 'Online',
        'IDLE': 'Idle',
        'DND': 'Do Not Disturb',
        'INVISIBLE': 'Offline',  # not a typo :)
        'OFFLINE': 'Offline',
    }

    def __init__(self, protocol, bot, config):
        self.protocol = protocol
        super().__init__(bot, config)

    @Plugin.listen('Ready')
    def on_ready(self, event, *args, **kwargs):
        self.botuser = event.user.id
        log.info('(%s) got ready event, starting messaging thread', self.protocol.name)
        self._message_thread = threading.Thread(name="Messaging thread for %s" % self.name,
                                                target=self.protocol._message_builder, daemon=True)
        self._message_thread.start()
        self.protocol.connected.set()

    def _burst_guild(self, guild):
        log.info('(%s) bursting guild %s/%s', self.protocol.name, guild.id, guild.name)
        pylink_netobj = self.protocol._create_child(guild.id, guild.name)
        pylink_netobj.uplink = None
        pylink_netobj._guild_name = guild.name

        pylink_netobj._burst_webhooks_agent()

        for member in guild.members.values():
            self._burst_new_client(guild, member, pylink_netobj)

        pylink_netobj.connected.set()
        pylink_netobj.call_hooks([None, 'ENDBURST', {}])

    def _update_channel_presence(self, guild, channel, member=None, *, relay_modes=False):
        """
        Updates channel presence & IRC modes for the given member, or all guild members if not given.
        """
        if channel.type == ChannelType.GUILD_CATEGORY:
            # XXX: there doesn't seem to be an easier way to get this. Fortunately, there usually
            # aren't too many channels in one guild...
            for subchannel in guild.channels.values():
                if subchannel.parent_id == channel.id:
                    log.debug('(%s) _update_channel_presence: checking channel %s/%s in category %s/%s', self.protocol.name, subchannel.id, subchannel, channel.id, channel)
                    self._update_channel_presence(guild, subchannel, member=member, relay_modes=relay_modes)
            return
        elif channel.type != ChannelType.GUILD_TEXT:
            log.debug('(%s) _update_channel_presence: ignoring non-text channel %s/%s', self.protocol.name, channel.id, channel)
            return

        modes = []
        users_joined = []

        try:
            pylink_netobj = self.protocol._children[guild.id]
        except KeyError:
            log.error("(%s) Could not update channel %s(%s)/%s as the parent network object does not exist", self.protocol.name, guild.id, guild.name, str(channel))
            return

        # Create a new channel if not present
        try:
            pylink_channel = pylink_netobj.channels[channel.id]
            pylink_channel.name = str(channel)
        except KeyError:
            pylink_channel = pylink_netobj.channels[channel.id] = Channel(self, name=str(channel))

        pylink_channel.discord_id = channel.id
        pylink_channel.discord_channel = channel

        if member is None:
            members = guild.members.values()
        else:
            members = [member]

        for member in members:
            uid = member.id
            try:
                pylink_user = pylink_netobj.users[uid]
            except KeyError:
                log.error("(%s) Could not update user %s(%s)/%s as the user object does not exist", self.protocol.name, guild.id, guild.name, uid)
                continue

            channel_permissions = channel.get_permissions(member)
            has_perm = channel_permissions.can(Permissions.read_messages)
            log.debug('discord: checking if member %s/%s has permission read_messages on %s/%s: %s',
                      member.id, member, channel.id, channel, has_perm)
            #log.debug('discord: channel permissions are %s', str(channel_permissions.to_dict()))
            if has_perm:
                if uid not in pylink_channel.users:
                    log.debug('discord: joining member %s to %s/%s', member, channel.id, channel)
                    pylink_user.channels.add(channel.id)
                    pylink_channel.users.add(uid)
                    users_joined.append(uid)

                for irc_mode, discord_permission in self.irc_discord_perm_mapping.items():
                    prefixlist = pylink_channel.prefixmodes[irc_mode] # channel.prefixmodes['op'] etc.
                    # If the user now has the permission but not the associated mode, add it to the mode list
                    has_op_perm = channel_permissions.can(discord_permission)
                    if has_op_perm:
                        modes.append(('+%s' % pylink_netobj.cmodes[irc_mode], uid))
                        if irc_mode == 'op':
                            # Stop adding lesser modes once we find an op; this reflects IRC services
                            # which tend to set +ao, +o, ... instead of +ohv, +aohv
                            break
                    elif (not has_op_perm) and uid in prefixlist:
                        modes.append(('-%s' % pylink_netobj.cmodes[irc_mode], uid))

            elif uid in pylink_channel.users and not has_perm:
                log.debug('discord: parting member %s from %s/%s', member, channel.id, channel)
                pylink_user.channels.discard(channel.id)
                pylink_channel.remove_user(uid)

                # We send KICK from a server to prevent triggering antiflood mechanisms...
                pylink_netobj.call_hooks([
                    guild.id,
                    'KICK',
                    {
                        'channel': channel.id,
                        'target': uid,
                        'text': "User removed from channel"
                    }
                ])

            # Optionally, burst the server owner as IRC owner
            if self.protocol.serverdata.get('show_owner_status', True) and uid == guild.owner_id:
                modes.append(('+q', uid))

        if modes:
            pylink_netobj.apply_modes(channel.id, modes)
            log.debug('(%s) Relaying permission changes on %s/%s as modes: %s', self.protocol.name, member.name,
                     channel, pylink_netobj.join_modes(modes))
            if relay_modes:
                pylink_netobj.call_hooks([guild.id, 'MODE', {'target': channel.id, 'modes': modes}])

        if users_joined:
            pylink_netobj.call_hooks([
                guild.id,
                'JOIN',
                {
                    'channel': channel.id,
                    'users': users_joined,
                    'modes': []
                }
            ])

    def _burst_new_client(self, guild, member, pylink_netobj):
        """Bursts the given member as a new PyLink client."""
        uid = member.id

        if not member.name:
            log.debug('(%s) Not bursting user %s as their data is not ready yet', self.protocol.name, member)
            return

        if uid in pylink_netobj.users:
            log.debug('(%s) Not reintroducing user %s/%s', self.protocol.name, uid, member.user.username)
            pylink_user = pylink_netobj.users[uid]
        else:
            tag = str(member.user)  # get their name#1234 tag
            username = member.user.username  # this is just the name portion
            realname = '%s @ Discord/%s' % (tag, guild.name)
            # Prefer the person's guild nick (nick=member.name) if defined
            pylink_netobj.users[uid] = pylink_user = User(pylink_netobj, nick=member.name,
                                                          ident=username, realname=realname,
                                                          host='discord/user/%s' % tag, # XXX make this configurable
                                                          ts=calendar.timegm(member.joined_at.timetuple()), uid=uid, server=guild.id)
            pylink_user.modes.add(('i', None))
            if member.user.bot:
                pylink_user.modes.add(('B', None))
            pylink_user.discord_user = member

            if uid == self.botuser:
                pylink_netobj.pseudoclient = pylink_user

            pylink_netobj.call_hooks([
                guild.id,
                'UID',
                {
                    'uid': uid,
                    'ts': pylink_user.ts,
                    'nick': pylink_user.nick,
                    'realhost': pylink_user.realhost,
                    'host': pylink_user.host,
                    'ident': pylink_user.ident,
                    'ip': pylink_user.ip
                }
            ])

        # Calculate which channels the user belongs to
        for channel in guild.channels.values():
            if channel.type == ChannelType.GUILD_TEXT:
                self._update_channel_presence(guild, channel, member)
        # Update user presence
        self._update_user_status(guild.id, uid, member.user.presence)
        return pylink_user

    @Plugin.listen('GuildCreate')
    def on_server_connect(self, event: events.GuildCreate, *args, **kwargs):
        log.info('(%s) got GuildCreate event for guild %s/%s', self.protocol.name, event.guild.id, event.guild.name)
        self._burst_guild(event.guild)

    @Plugin.listen('GuildUpdate')
    def on_server_update(self, event: events.GuildUpdate, *args, **kwargs):
        log.info('(%s) got GuildUpdate event for guild %s/%s', self.protocol.name, event.guild.id, event.guild.name)
        try:
            pylink_netobj = self.protocol._children[event.guild.id]
        except KeyError:
            log.error("(%s) Could not update guild %s/%s as the corresponding network object does not exist", self.protocol.name, event.guild.id, event.guild.name)
            return
        else:
            pylink_netobj._guild_name = event.guild.name

    @Plugin.listen('GuildDelete')
    def on_server_delete(self, event: events.GuildDelete, *args, **kwargs):
        log.info('(%s) Got kicked from guild %s, triggering a disconnect', self.protocol.name, event.id)
        self.protocol._remove_child(event.id)

    @Plugin.listen('GuildMembersChunk')
    def on_member_chunk(self, event: events.GuildMembersChunk, *args, **kwargs):
        log.debug('(%s) got GuildMembersChunk event for guild %s/%s: %s', self.protocol.name, event.guild.id, event.guild.name, event.members)
        try:
            pylink_netobj = self.protocol._children[event.guild.id]
        except KeyError:
            log.error("(%s) Could not burst users %s as the parent network object does not exist", self.protocol.name, event.members)
            return

        for member in event.members:
            self._burst_new_client(event.guild, member, pylink_netobj)

    @Plugin.listen('GuildMemberAdd')
    def on_member_add(self, event: events.GuildMemberAdd, *args, **kwargs):
        log.info('(%s) got GuildMemberAdd event for guild %s/%s: %s', self.protocol.name, event.guild.id, event.guild.name, event.member)
        try:
            pylink_netobj = self.protocol._children[event.guild.id]
        except KeyError:
            log.error("(%s) Could not burst user %s as the parent network object does not exist", self.protocol.name, event.member)
            return
        self._burst_new_client(event.guild, event.member, pylink_netobj)

    @Plugin.listen('GuildMemberUpdate')
    def on_member_update(self, event: events.GuildMemberUpdate, *args, **kwargs):
        log.info('(%s) got GuildMemberUpdate event for guild %s/%s: %s', self.protocol.name, event.guild.id, event.guild.name, event.member)
        try:
            pylink_netobj = self.protocol._children[event.guild.id]
        except KeyError:
            log.error("(%s) Could not update user %s as the parent network object does not exist", self.protocol.name, event.member)
            return

        uid = event.member.id
        pylink_user = pylink_netobj.users.get(uid)
        if not pylink_user:
            self._burst_new_client(event.guild, event.member, pylink_netobj)
            return

        # Handle NICK changes
        oldnick = pylink_user.nick
        if pylink_user.nick != event.member.name:
            pylink_user.nick = event.member.name
            pylink_netobj.call_hooks([uid, 'NICK', {'newnick': event.member.name, 'oldnick': oldnick}])

        # Relay permission changes as modes
        for channel in event.guild.channels.values():
            if channel.type == ChannelType.GUILD_TEXT:
                self._update_channel_presence(event.guild, channel, event.member, relay_modes=True)

    @Plugin.listen('GuildMemberRemove')
    def on_member_remove(self, event: events.GuildMemberRemove, *args, **kwargs):
        log.info('(%s) got GuildMemberRemove event for guild %s: %s', self.protocol.name, event.guild_id, event.user)
        try:
            pylink_netobj = self.protocol._children[event.guild_id]
        except KeyError:
            log.debug("(%s) Could not remove user %s as the parent network object does not exist", self.protocol.name, event.user)
            return

        if event.user.id in pylink_netobj.users:
            pylink_netobj._remove_client(event.user.id)
            # XXX: make the message configurable
            pylink_netobj.call_hooks([event.user.id, 'QUIT', {'text': 'User left the guild'}])

    @Plugin.listen('ChannelCreate')
    @Plugin.listen('ChannelUpdate')
    def on_channel_update(self, event, *args, **kwargs):
        # XXX: disco should be doing this for us?!
        if event.overwrites:
            log.debug('discord: resetting channel overrides on %s/%s: %s', event.channel.id, event.channel, event.overwrites)
            event.channel.overwrites = event.overwrites
        # Update channel presence via permissions for EVERYONE!
        self._update_channel_presence(event.channel.guild, event.channel, relay_modes=True)

    @Plugin.listen('ChannelDelete')
    def on_channel_delete(self, event, *args, **kwargs):
        channel = event.channel
        try:
            pylink_netobj = self.protocol._children[event.channel.guild_id]
        except KeyError:
            log.debug("(%s) Could not delete channel %s as the parent network object does not exist", self.protocol.name, event.channel)
            return

        if channel.id not in pylink_netobj.channels:  # wasn't a type of channel we track
            return

        # Remove the channel from everyone's channel list
        for u in pylink_netobj.channels[channel.id].users:
            pylink_netobj.users[u].channels.discard(channel.id)
        del pylink_netobj.channels[channel.id]

    @Plugin.listen('MessageCreate')
    def on_message(self, event: events.MessageCreate, *args, **kwargs):
        message = event.message
        subserver = None
        target = None

        # If the bot is the one sending the message, don't do anything
        if message.author.id == self.botuser:
            return

        if not message.guild:
            # This is a DM
            # see if we've seen this user on any of our servers
            for discord_sid, server in self.protocol._children.items():
                if message.author.id in server.users:
                    target = self.botuser
                    subserver = discord_sid
                    #server.users[message.author.id].dm_channel = message.channel
                    #server.channels[message.channel.id] = c  # Create a new channel
                    #c.discord_channel = message.channel

                    break
            if not (subserver or target):
                return
        else:
            subserver = message.guild.id
            target = message.channel.id

        if not subserver:
            return

        def format_user_mentions(u):
            # Try to find the user's guild nick, falling back to the user if that fails
            if message.guild and u.id in message.guild.members:
                return '@' + message.guild.members[u.id].name
            else:
                return '@' + str(u)

            # Translate mention IDs to their names

        text = message.replace_mentions(user_replace=format_user_mentions,
                                        role_replace=lambda r: '@' + str(r),
                                        channel_replace=str)
        try:
            text = D2IFormatter().format(text)
        except:
            log.exception('(%s) Error translating from Discord to IRC: %s', self.name, text)

        pylink_netobj = self.protocol._children[subserver]
        author = message.author.id
        if message.webhook_id:
            if not pylink_netobj.webhooks_agent_uid or \
                    (hasattr(pylink_netobj.channels[target], 'webhook') and
                     message.webhook_id == pylink_netobj.channels[target].webhook.id):
                return

            author = pylink_netobj.webhooks_agent_uid
            # Format the message to contain the webhook username
            text = '<{}> {}'.format(message.author.username, text)

            # Join the webhooks agent as needed to the channel.
            if pylink_netobj.webhooks_agent_uid not in pylink_netobj.channels[target].users:
                pylink_netobj.join(pylink_netobj.webhooks_agent_uid, target)
                pylink_netobj.call_hooks([subserver, 'JOIN',
                                          {'channel': target,
                                           'users': [pylink_netobj.webhooks_agent_uid],
                                           'modes': []
                                          }])
        def _send(text):
            for line in text.split('\n'):  # Relay multiline messages as such
                pylink_netobj.call_hooks([author, 'PRIVMSG', {'target': target, 'text': line}])

        _send(text)
        # For attachments, just send the link
        for attachment in message.attachments.values():
            _send(attachment.url)

    def _update_user_status(self, guild_id, uid, presence):
        """Handles a Discord presence update."""
        pylink_netobj = self.protocol._children.get(guild_id)
        if pylink_netobj:
            try:
                u = pylink_netobj.users[uid]
            except KeyError:
                log.debug('(%s) _update_user_status: could not fetch user %s', self.protocol.name, uid, exc_info=True)
                return
            # It seems that presence updates are not sent at all for offline users, so they
            # turn into an unset field in disco. I guess this makes sense for saving bandwidth?
            if presence:
                status = presence.status
            else:
                status = DiscordStatus.OFFLINE

            if status != DiscordStatus.ONLINE:
                awaymsg = self.status_mapping.get(status.value, 'Unknown Status')
            else:
                awaymsg = ''

            u.away = awaymsg
            pylink_netobj.call_hooks([uid, 'AWAY', {'text': awaymsg}])

    @Plugin.listen('PresenceUpdate')
    def on_presence_update(self, event, *args, **kwargs):
        self._update_user_status(event.guild_id, event.presence.user.id, event.presence)

class DiscordServer(ClientbotBaseProtocol):
    S2S_BUFSIZE = 0

    def __init__(self, _, parent, server_id, guild_name):
        self.sid = server_id  # Allow serverdata to work first
        self.virtual_parent = parent

        # Try to find a predefined server name; if that fails, use the server id.
        # We don't use the guild name as the PyLink network name because they can be
        # changed at any time, which will break plugins that store data per network.
        fallback_name = 'd%d' % server_id
        name = self.serverdata.get('name', fallback_name)
        if name in world.networkobjects:
            raise ValueError("Attempting to reintroduce network with name %r" % name)

        super().__init__(name)
        self.sidgen = PUIDGenerator('DiscordInternalSID')
        self.uidgen = PUIDGenerator('PUID')

        self.servers[self.sid] = Server(self, None, server_id, internal=False, desc=guild_name)

    def _init_vars(self):
        super()._init_vars()
        self.casemapping = 'ascii'  # TODO: investigate utf-8 support
        self.cmodes = {'op': 'o', 'halfop': 'h', 'voice': 'v', 'owner': 'q', 'admin': 'a',
                       '*A': '', '*B': '', '*C': '', '*D': ''}
        self.umodes = {'invisible': 'i', 'bot': 'B'}
        # The actual prefix chars don't matter; we just need to make sure +qaohv are in
        # the prefixmodes list so that the mode internals work properly
        self.prefixmodes = {'q': '~', 'a': '&', 'o': '@', 'h': '%', 'v': '+'}

        # Use an instance of DiscordChannelState, which converts string forms of channels to int
        self._channels = self.channels = DiscordChannelState()

    @property
    def serverdata(self):
        """
        Implements serverdata property for Discord subservers. This merges in the root serverdata config
        block, plus any guild-specific settings for this guild.
        """
        if getattr(self, 'sid', None):
            data = self.virtual_parent.serverdata.copy()
            guild_data = data.get('guilds', {}).get(self.sid, {})
            log.debug('serverdata: merging data %s with guild_data %s', data, guild_data)
            data.update(guild_data)
            return data
        else:
            log.debug('serverdata: sid not set, using parent data only')
            return self.virtual_parent.serverdata

    @serverdata.setter
    def serverdata(self, value):
        if getattr(self, 'sid', None):
            data = self.virtual_parent.serverdata
            # Create keys if not existing
            if 'guilds' not in data:
                data['guilds'] = {}
            if self.sid not in data['guilds']:
                data['guilds'][self.sid] = {}
            data['guilds'][self.sid].update(value)
        else:
            raise RuntimeError('Cannot set serverdata because self.sid points nowhere')

    def is_nick(self, *args, **kwargs):
        return self.virtual_parent.is_nick(*args, **kwargs)

    def is_channel(self, *args, **kwargs):
        """Returns whether the target is a channel."""
        return self.virtual_parent.is_channel(*args, **kwargs)

    def is_server_name(self, *args, **kwargs):
        """Returns whether the string given is a valid IRC server name."""
        return self.virtual_parent.is_server_name(*args, **kwargs)

    def get_friendly_name(self, *args, **kwargs):
        """
        Returns the friendly name of a SID (the guild name), UID (the nick), or channel (the name).
        """
        return self.virtual_parent.get_friendly_name(*args, caller=self, **kwargs)

    def get_full_network_name(self):
        """
        Returns the guild name.
        """
        return 'Discord/' + self._guild_name

    WEBHOOKS_AGENT_REALNAME = 'pylink-discord webhooks agent'
    def _burst_webhooks_agent(self):
        webhooks_agent = self.serverdata.get('webhooks_agent')
        if not webhooks_agent:
            log.debug('(%s) Not bursting webhooks agent; it is not enabled', self.name)
            return

        try:  # Try to treat it as a hostmask
            webhooks_agent = utils.split_hostmask(webhooks_agent)
        except ValueError:  # If that fails, treat it as just a nick
            webhooks_agent = [webhooks_agent, 'webhooks', 'discord/webhooks-agent']
        agent_uid = self.webhooks_agent_uid = self.uidgen.next_uid()

        log.debug('(%s) Bursting webhooks agent as %s!%s@%s', self.name, *webhooks_agent)
        self.users[agent_uid] = pylink_user = User(self, nick=webhooks_agent[0],
                                                   ident=webhooks_agent[1], realname=self.WEBHOOKS_AGENT_REALNAME,
                                                   host=webhooks_agent[2],
                                                   ts=int(time.time()),
                                                   uid=agent_uid,
                                                   server=self.sid)
        pylink_user.modes.update({('i', None), ('B', None)})

        return agent_uid

    def message(self, source, target, text, notice=False):
        """Sends messages to the target."""
        if target in self.users:
            userobj = self.users[target]
            # Get or create the DM channel for this user
            self.users[target].dm_channel = getattr(userobj, 'dm_channel', userobj.discord_user.user.open_dm())
            discord_target = self.users[target].dm_channel

        elif target in self.channels:
            discord_target = self.channels[target].discord_channel
        else:
            log.error('(%s) Could not find message target for %s', self.name, target)
            return

        message_data = {'target': discord_target, 'sender': source, 'text': text}
        if self.pseudoclient and self.pseudoclient.uid == source:
            self.virtual_parent.message_queue.put_nowait(message_data)
            return

        # Try webhook only if the target is linked to a guild and servers::<discord>::guilds::<guild_id>::use_webhooks is true
        if discord_target.guild and self.serverdata.get('use_webhooks', False):
            try:
                webhook = self._get_or_create_webhook(self.channels[target].discord_id)
            except APIException:
                log.debug('(%s) _get_or_create_webhook: could not get or create webhook for channel %s. Falling back to standard Clientbot behavior',
                          self.name, target, exc_info=True)
                webhook = None
            if webhook:

                try:
                    remotenet, remoteuser = self.users[source].remote
                    remoteirc = world.networkobjects[remotenet]
                    message_data.update(self._get_user_webhook_data(remoteirc, remoteuser))
                except (KeyError, ValueError):
                    log.debug('(%s) Failure getting user info for user %s. Falling back to standard Clientbot behavior',
                              self.name, source, exc_info=True)
                else:
                    if text.startswith('\x01ACTION'):  # Mangle IRC CTCP actions
                        try:
                            message_data['text'] = '* %s%s' % (remoteirc.get_friendly_name(remoteuser), text[len('\x01ACTION'):-1])
                        except (KeyError, ValueError):
                            log.exception('(%s) Failed to normalize IRC CTCP action: source=%s, text=%s', self.name, source, text)
                    elif text.startswith('\x01'):
                        return  # Drop other CTCPs

                    message_data['webhook'] = webhook
                    self.virtual_parent.message_queue.put_nowait(message_data)
                    return

        self.call_hooks([source, 'CLIENTBOT_MESSAGE', {'target': target, 'is_notice': notice, 'text': text}])

    def join(self, client, channel):
        """STUB: Joins a user to a channel."""
        if self.pseudoclient and client == self.pseudoclient.uid:
            log.debug("(%s) discord: ignoring explicit channel join to %s", self.name, channel)
            return
        elif channel not in self.channels:
            log.warning("(%s) Ignoring attempt to join unknown channel ID %s", self.name, channel)
            return
        self.channels[channel].users.add(client)
        self.users[client].channels.add(channel)

        log.debug('(%s) join: faking JOIN of client %s/%s to %s', self.name, client,
                  self.get_friendly_name(client), channel)
        self.call_hooks([client, 'CLIENTBOT_JOIN', {'channel': channel}])

    def send(self, data, queue=True):
        log.debug('(%s) Ignoring attempt to send raw data via child network object', self.name)
        return

    @staticmethod
    def wrap_message(source, target, text):
        """
        STUB: returns the message text wrapped onto multiple lines.
        """
        return [text]

    def _get_or_create_webhook(self, channel_id):
        webhook = getattr(self.channels[channel_id], 'webhook', None)
        if webhook:
            return webhook

        webhook_user = self.serverdata.get('webhook_name') or 'PyLinkRelay'
        webhook_user += '-%d' % channel_id
        for webhook in self.virtual_parent.client.api.channels_webhooks_list(channel_id):
            if webhook.name == webhook_user:
                self.channels[channel_id].webhook = webhook
                return webhook
        else:
            log.info('(%s) Creating new web-hook on channel %s/%s', self.name, channel_id, self.get_friendly_name(channel_id))
            return self.virtual_parent.client.api.channels_webhooks_create(channel_id, name=webhook_user)

    def _get_user_webhook_data(self, netobj, uid):
        user = netobj.users[uid]

        fields = user.get_fields()
        fields['netname'] = netobj.get_full_network_name()
        fields['nettag'] = netobj.name

        user_format = self.serverdata.get('webhook_user_format', "$nick @ IRC/$netname")
        tmpl = string.Template(user_format)
        data = {
            'username': tmpl.safe_substitute(fields)
        }

        default_avatar_url = self.serverdata.get('default_avatar_url')

        # XXX: we'll have a more rigorous matching system later on
        if user.services_account in self.serverdata.get('avatars', {}):
            avatar_url = self.serverdata['avatars'][user.services_account]
            p = urllib.parse.urlparse(avatar_url)
            log.debug('(%s) Got raw avatar URL %s for UID %s', self.name, avatar_url, uid)

            if p.scheme == 'gravatar' and libgravatar:  # gravatar:hello@example.com
                try:
                    g = libgravatar.Gravatar(p.path)
                    log.debug('(%s) Using Gravatar email %s for UID %s', self.name, p.path, uid)
                    data['avatar_url'] = g.get_image(use_ssl=True)
                except:
                    log.exception('Failed to obtain Gravatar image for user %s/%s', uid, p.path)

            elif p.scheme in ('http', 'https'):  # a direct image link
                data['avatar_url'] = avatar_url

            else:
                log.warning('(%s) Unknown avatar URI %s for UID %s', self.name, avatar_url, uid)
        elif default_avatar_url:
            log.debug('(%s) Avatar not defined for UID %s; using default avatar %s', self.name, uid, default_avatar_url)
            data['avatar_url'] = default_avatar_url
        else:
            log.debug('(%s) Avatar not defined for UID %s; using default webhook avatar', self.name, uid)

        return data

class PyLinkDiscordProtocol(PyLinkNetworkCoreWithUtils):
    S2S_BUFSIZE = 0

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)

        if 'token' not in self.serverdata:
            raise ProtocolError("No API token defined under server settings")
        self.client_config = ClientConfig({'token': self.serverdata['token']})
        self.client = Client(self.client_config)
        self.bot_config = BotConfig()
        self.bot = Bot(self.client, self.bot_config)
        self.bot_plugin = DiscordBotPlugin(self, self.bot, self.bot_config)
        self.bot.add_plugin(self.bot_plugin)
        #setup_logging(level='DEBUG')
        self._children = {}
        self.message_queue = queue.Queue()

    @staticmethod
    def is_nick(s, nicklen=None):
        return True

    def is_channel(self, s):
        """Returns whether the target is a channel."""
        try:
            chan = int(s)
        except (TypeError, ValueError):
            return False
        return chan in self.bot_plugin.state.channels

    def is_server_name(self, s):
        """Returns whether the string given is a valid IRC server name."""
        return s in self.bot_plugin.state.guilds

    def get_friendly_name(self, entityid, caller=None):
        """
        Returns the friendly name of a SID (the guild name), UID (the nick), or channel (the name).
        """
        # internal PUID, handle appropriately
        if isinstance(entityid, str) and '@' in entityid:
            if entityid in self.users:
                return self.users[entityid].nick
            elif caller and entityid in caller.users:
                return caller.users[entityid].nick
            else:
                # Probably a server
                return entityid.split('@', 1)[0]

        if self.is_channel(entityid):
            return str(self.bot_plugin.state.channels[entityid])
        elif entityid in self.bot_plugin.state.users:
            return self.bot_plugin.state.users[entityid].username
        elif self.is_server_name(entityid):
            return self.bot_plugin.state.guilds[entityid].name
        else:
            raise KeyError("Unknown entity ID %s" % str(entityid))

    @staticmethod
    def wrap_message(source, target, text):
        """
        STUB: returns the message text wrapped onto multiple lines.
        """
        return [text]

    def _message_builder(self):
        current_channel_senders = {}
        joined_messages = collections.defaultdict(dict)
        while not self._aborted.is_set():
            try:
                message = self.message_queue.get(timeout=BATCH_DELAY)
                message_text = message.pop('text', '')

                try:
                    message_text = I2DFormatter().format(message_text)
                except:
                    log.exception('(%s) Error translating from IRC to Discord: %s', self.name, message_text)
                channel = message.pop('target')
                current_sender = current_channel_senders.get(channel, None)

                if current_sender != message['sender']:
                     self.flush(channel, joined_messages[channel])
                     joined_messages[channel] = message

                current_channel_senders[channel] = message['sender']

                joined_message = joined_messages[channel].get('text', '')
                joined_messages[channel]['text'] = joined_message + "\n{}".format(message_text)
            except queue.Empty:
                for channel, message_info in joined_messages.items():
                    self.flush(channel, message_info)
                joined_messages.clear()
                current_channel_senders.clear()

    def flush(self, channel, message_info):
        try:
            message_text = message_info.pop('text', '').strip()
            if message_text:
                if message_info.get('username'):
                    log.debug('(%s) Sending webhook to channel %s with user %s and avatar %s', self.name, channel,
                              message_info.get('username'), message_info.get('avatar_url'))
                    message_info['webhook'].execute(
                        content=message_text,
                        username=message_info['username'],
                        avatar_url=message_info.get('avatar_url'),
                    )
                else:
                    channel.send_message(message_text)
        except:
            log.exception('(%s) Error sending message:', self.name)

    def _create_child(self, server_id, guild_name):
        """
        Creates a virtual network object for a server with the given name.
        """
        # This is a bit different because we let the child server find its own name
        # and report back to us.
        child = DiscordServer(None, self, server_id, guild_name)
        world.networkobjects[child.name] = self._children[server_id] = child
        return child

    def _remove_child(self, server_id):
        """
        Removes a virtual network object with the given name.
        """
        pylink_netobj = self._children[server_id]
        pylink_netobj.call_hooks([None, 'PYLINK_DISCONNECT', {}])
        del self._children[server_id]
        del world.networkobjects[pylink_netobj.name]

    def connect(self):
        self._aborted.clear()
        self.client.run()

    def disconnect(self):
        """Disconnects from Discord and shuts down this network object."""
        self._aborted.set()

        self._pre_disconnect()

        children = self._children.copy()
        for child in children:
            self._remove_child(child)

        self.client.gw.shutting_down = True
        self.client.gw.ws.close()

        self._post_disconnect()

Class = PyLinkDiscordProtocol
