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
import operator
import collections

from disco.bot import Bot, BotConfig
from disco.bot import Plugin
from disco.client import Client, ClientConfig
from disco.gateway.events import *
from disco.types import Guild, Channel as DiscordChannel, GuildMember, Message
from disco.types.channel import ChannelType
from disco.types.permissions import Permissions
from disco.types.user import Status as DiscordStatus
#from disco.util.logging import setup_logging
from holster.emitter import Priority

from pylinkirc import structures
from pylinkirc.classes import *
from pylinkirc.log import log
from pylinkirc.protocols.clientbot import ClientbotBaseProtocol

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
    def on_server_connect(self, event: GuildCreate, *args, **kwargs):
        log.info('(%s) got GuildCreate event for guild %s/%s', self.protocol.name, event.guild.id, event.guild.name)
        self._burst_guild(event.guild)

    @Plugin.listen('GuildDelete')
    def on_server_delete(self, event: GuildDelete, *args, **kwargs):
        log.info('(%s) Got kicked from guild %s, triggering a disconnect', self.protocol.name, event.id)
        self.protocol._remove_child(event.id)

    @Plugin.listen('GuildMembersChunk')
    def on_member_chunk(self, event: GuildMembersChunk, *args, **kwargs):
        log.info('(%s) got GuildMembersChunk event for guild %s/%s: %s', self.protocol.name, event.guild.id, event.guild.name, event.members)
        try:
            pylink_netobj = self.protocol._children[event.guild.id]
        except KeyError:
            log.error("(%s) Could not burst users %s as the parent network object does not exist", self.protocol.name, event.members)
            return

        for member in event.members:
            self._burst_new_client(event.guild, member, pylink_netobj)

    @Plugin.listen('GuildMemberAdd')
    def on_member_add(self, event: GuildMemberAdd, *args, **kwargs):
        log.info('(%s) got GuildMemberAdd event for guild %s/%s: %s', self.protocol.name, event.guild.id, event.guild.name, event.member)
        try:
            pylink_netobj = self.protocol._children[event.guild.id]
        except KeyError:
            log.error("(%s) Could not burst user %s as the parent network object does not exist", self.protocol.name, event.member)
            return
        self._burst_new_client(event.guild, event.member, pylink_netobj)

    @Plugin.listen('GuildMemberUpdate')
    def on_member_update(self, event: GuildMemberUpdate, *args, **kwargs):
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
    def on_member_remove(self, event: GuildMemberRemove, *args, **kwargs):
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

    @staticmethod
    def _format_embed(embed):
        return '%s - %s (%s) \x02<%s>\x02' % (embed.title, embed.description, embed.description, embed.url)

    @staticmethod
    def _format_attachment(attachment):
        # TODO: humanize attachment sizes
        #return 'Attachment: %s (%s) \x02<%s>\x02' % (attachment.filename, attachment.size, attachment.proxy_url)
        return 'Attachment: %s \x02<%s>\x02' % (attachment.filename, attachment.url)

    @Plugin.listen('MessageCreate')
    def on_message(self, event: MessageCreate, *args, **kwargs):
        message = event.message
        subserver = None
        target = None

        # If the bot is the one sending the message, don't do anything
        if message.author.id == self.botuser or message.webhook_id:
            return

        if not message.guild:
            # This is a DM
            # see if we've seen this user on any of our servers
            for discord_sid, server in self.protocol._children.items():
                if message.author.id in server.users:
                    target = self.botuser
                    subserver = discord_sid
                    server.users[message.author.id].dm_channel = message.channel
                    #server.channels[message.channel.id] = c  # Create a new channel
                    #c.discord_channel = message.channel

                    break
            if not (subserver or target):
                return
        else:
            subserver = message.guild.id
            target = message.channel.id

        if subserver:
            pylink_netobj = self.protocol._children[subserver]
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
            text = D2IFormatter().format(text)  # Translate IRC formatting to Discord
            def _send(text):
                for line in text.splitlines():  # Relay multiline messages as such
                    pylink_netobj.call_hooks([message.author.id, 'PRIVMSG', {'target': target, 'text': line}])

            _send(text)
            # Throw in each embed and attachment as a separate IRC line
            for embed in message.embeds:
                _send(self._format_embed(embed))
            for attachment in message.attachments.values():
                _send(self._format_attachment(attachment))

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

    def __init__(self, name, parent, server_id, guild_name):
        conf.conf['servers'][name] = {'netname': 'Discord/%s' % guild_name}
        super().__init__(name)
        self.virtual_parent = parent
        self.sidgen = PUIDGenerator('DiscordInternalSID')
        self.uidgen = PUIDGenerator('PUID')
        self.sid = server_id
        self.servers[self.sid] = Server(self, None, server_id, internal=False, desc=name)

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

    def message(self, source, target, text, notice=False):
        """Sends messages to the target."""
        if target in self.users:
            try:
                discord_target = self.users[target].dm_channel
            except ValueError:
                discord_target = self.users[target].dm_channel = self.users[target].discord_user.user.open_dm()

        elif target in self.channels:
            discord_target = self.channels[target].discord_channel
        else:
            log.error('(%s) Could not find message target for %s', self.name, target)
            return

        message_data = {'target': discord_target, 'sender': source}
        if self.pseudoclient and self.pseudoclient.uid == source:
            message_data['text'] = I2DFormatter().format(text)
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
                channel = message.pop('target')
                current_sender = current_channel_senders.get(channel, None)

                # We'll enable this when we work on webhook support again...
                #if current_sender != message['sender']:
                #    self.flush(channel, joined_messages[channel])
                #    joined_messages[channel] = message

                current_channel_senders[channel] = message['sender']

                joined_message = joined_messages[channel].get('text', '')
                joined_messages[channel]['text'] = joined_message + "\n{}".format(message_text)
            except queue.Empty:
                for channel, message_info in joined_messages.items():
                    self.flush(channel, message_info)
                joined_messages.clear()
                current_channel_senders.clear()

    def flush(self, channel, message_info):
        message_text = message_info.pop('text', '').strip()
        if message_text:
            if message_info.get('username'):
                message_info['webhook'].execute(
                    content=message_text,
                    username=message_info['username'],
                    avatar_url=message_info.get('avatar'),
                    )
            else:
                channel.send_message(message_text)

    def _create_child(self, server_id, guild_name):
        """
        Creates a virtual network object for a server with the given name.
        """
        # Try to find a predefined server name; if that fails, use the server id.
        # We don't use the guild name here because those can be changed at any time,
        # confusing plugins that store data by PyLink network names.
        fallback_name = 'd%d' % server_id
        name = self.serverdata.get('server_names', {}).get(server_id, fallback_name)

        if name in world.networkobjects:
            raise ValueError("Attempting to reintroduce network with name %r" % name)
        child = DiscordServer(name, self, server_id, guild_name)
        world.networkobjects[name] = self._children[server_id] = child
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
