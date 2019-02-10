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

import websocket
from disco.bot import Bot, BotConfig
from disco.bot import Plugin
from disco.client import Client, ClientConfig
from disco.gateway.events import *
from disco.types import Guild, Channel as DiscordChannel, GuildMember, Message
from disco.types.channel import ChannelType
from disco.types.permissions import Permissions
#from disco.util.logging import setup_logging
from holster.emitter import Priority
from pylinkirc.classes import *
from pylinkirc.log import log
from pylinkirc.protocols.clientbot import ClientbotBaseProtocol

from ._discord_formatter import I2DFormatter

websocket.enableTrace(True)

BATCH_DELAY = 0.3  # TODO: make this configurable

class DiscordBotPlugin(Plugin):
    # TODO: maybe this could be made configurable?
    # N.B. iteration order matters: we stop adding lower modes once someone has +o, much like
    #      real services
    irc_discord_perm_mapping = collections.OrderedDict(
        [('admin', Permissions.ADMINISTRATOR),
         ('op', Permissions.BAN_MEMBERS),
         ('halfop', Permissions.KICK_MEMBERS),
         ('voice', Permissions.SEND_MESSAGES),
        ])
    botuser = None

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
        pylink_netobj = self.protocol._create_child(guild.name, guild.id)
        pylink_netobj.uplink = None

        for member in guild.members.values():
            self._burst_new_client(guild, member, pylink_netobj)

        pylink_netobj.connected.set()
        pylink_netobj.call_hooks([None, 'ENDBURST', {}])

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
            pylink_user.discord_user = member

            if uid == self.botuser:
                pylink_netobj.pseudoclient = pylink_user

            pylink_netobj.call_hooks([
                None,
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

        guild_permissions = guild.get_permissions(member)
        # Calculate which channels the user belongs to
        for channel in guild.channels.values():
            if channel.type == ChannelType.GUILD_TEXT:
                modes = []
                pylink_channame = '#' + channel.name
                # Automatically create a channel if not present
                pylink_channel = pylink_netobj._channels[pylink_channame]
                pylink_channel.discord_channel = channel

                # We consider a user to be "in a channel" if they are allowed to read messages there
                # XXX we shouldn't need to check both??
                channel_permissions = channel.get_permissions(member)
                log.debug('discord: checking if member %s has permission read_messages on %s/%s: %s',
                          member, channel.id, pylink_channame, channel_permissions.can(Permissions.READ_MESSAGES))
                log.debug('discord: checking if member %s has permission read_messages on guild %s/%s: %s',
                          member, guild.id, guild.name, guild_permissions.can(Permissions.READ_MESSAGES))
                if channel_permissions.can(Permissions.read_messages) or guild_permissions.can(Permissions.READ_MESSAGES):
                    pylink_user.channels.add(pylink_channame)
                    pylink_channel.users.add(uid)

                    for irc_mode, discord_permission in self.irc_discord_perm_mapping.items():
                        if channel_permissions.can(discord_permission) or guild_permissions.can(discord_permission):
                            modes.append(('+%s' % pylink_netobj.cmodes[irc_mode], uid))
                            if irc_mode == 'op':
                                # Stop adding lesser modes once we find an op; this reflects IRC services
                                # which tend to set +ao, +o, ... instead of +ohv, +aohv
                                break

                    if modes:
                        pylink_netobj.apply_modes(pylink_channame, modes)

                    pylink_netobj.call_hooks([
                        None,
                        'JOIN',
                        {
                            'channel': pylink_channame,
                            'users': [uid],
                            'modes': []
                        }
                    ])

        return pylink_user

    @Plugin.listen('GuildCreate')
    def on_server_connect(self, event: GuildCreate, *args, **kwargs):
        log.info('(%s) got GuildCreate event for guild %s/%s', self.protocol.name, event.guild.id, event.guild.name)
        self._burst_guild(event.guild)

    @Plugin.listen('GuildMembersChunk')
    def on_member_chunk(self, event: GuildMembersChunk, *args, **kwargs):
        log.info('(%s) got GuildMembersChunk event for guild %s/%s: %s', self.protocol.name, event.guild.id, event.guild.name, event.members)
        try:
            pylink_netobj = self.protocol._children[event.guild.name]
        except KeyError:
            log.error("(%s) Could not burst users %s as the parent network object does not exist", self.protocol.name, event.members)
            return

        for member in event.members:
            self._burst_new_client(event.guild, member, pylink_netobj)

    @Plugin.listen('GuildMemberAdd')
    def on_member_add(self, event: GuildMemberAdd, *args, **kwargs):
        log.info('(%s) got GuildMemberAdd event for guild %s/%s: %s', self.protocol.name, event.guild.id, event.guild.name, event.member)
        try:
            pylink_netobj = self.protocol._children[event.guild.name]
        except KeyError:
            log.error("(%s) Could not burst user %s as the parent network object does not exist", self.protocol.name, event.member)
            return
        self._burst_new_client(event.guild, event.member, pylink_netobj)

    @Plugin.listen('GuildMemberUpdate')
    def on_member_update(self, event: GuildMemberUpdate, *args, **kwargs):
        log.info('(%s) got GuildMemberUpdate event for guild %s/%s: %s', self.protocol.name, event.guild.id, event.guild.name, event.member)
        try:
            pylink_netobj = self.protocol._children[event.guild.name]
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
        guild_permissions = event.guild.get_permissions(event.member)
        for channel in event.guild.channels.values():
           if channel.type == ChannelType.GUILD_TEXT:
               pylink_channame = '#' + channel.name
               if pylink_channame not in pylink_netobj.channels:
                   log.warning("(%s) Possible desync? Can't update modes on channel %s/%s because it does not exist in the PyLink state",
                               pylink_netobj.name, channel.id, pylink_channame)
                   continue

               c = pylink_netobj.channels[pylink_channame]
               channel_permissions = channel.get_permissions(event.member)
               modes = []

               for irc_mode, discord_permission in self.irc_discord_perm_mapping.items():
                   prefixlist = c.prefixmodes[irc_mode] # irc.prefixmodes['op'] etc.
                   has_perm = channel_permissions.can(discord_permission) or guild_permissions.can(discord_permission)

                   # If the user now has the permission but not the mode, add it to the mode list
                   if has_perm and uid not in prefixlist:
                       modes.append(('+%s' % pylink_netobj.cmodes[irc_mode], uid))
                       if irc_mode == 'op':
                           # Stop adding lesser modes once we find an op; this reflects IRC services
                           # which tend to set +ao, +o, ... instead of +ohv, +aohv
                           break
                   # If the user had the permission removed, remove it from the mode list
                   elif (not has_perm) and uid in prefixlist:
                       modes.append(('-%s' % pylink_netobj.cmodes[irc_mode], uid))

               if modes:
                   pylink_netobj.apply_modes(pylink_channame, modes)
                   log.debug('(%s) Relaying permission changes on %s/%s as modes: %s', self.protocol.name, event.member.name,
                             pylink_channame, pylink_netobj.join_modes(modes))
                   pylink_netobj.call_hooks([None, 'MODE', {'target': pylink_channame, 'modes': modes}])

    @Plugin.listen('GuildMemberRemove')
    def on_member_remove(self, event: GuildMemberRemove, *args, **kwargs):
        log.info('(%s) got GuildMemberRemove event for guild %s/%s: %s', self.protocol.name, event.guild.id, event.guild.name, event.user)
        try:
            pylink_netobj = self.protocol._children[event.guild.name]
        except KeyError:
            log.error("(%s) Could not remove user %s as the parent network object does not exist", self.protocol.name, event.user)
            return

        if event.user.id in pylink_netobj.users:
            pylink_netobj._remove_client(event.user.id)
            # XXX: make the message configurable
            pylink_netobj.call_hooks([event.user.id, 'QUIT', {'text': 'User left the guild'}])

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
            for server in self.protocol._children.values():
                if message.author.id in server.users:
                    target = self.botuser
                    subserver = server.name
                    #server.users[message.author.id].dm_channel = message.channel
                    #server._channels[message.channel.id] = c  # Create a new channel
                    #c.discord_channel = message.channel

                    break
            if not (subserver or target):
                return
        else:
            subserver = message.guild.name
            # For plugins, route channel targets to the name instead of ID
            target = '#' + message.channel.name

        if subserver:
            pylink_netobj = self.protocol._children[subserver]
            pylink_netobj.call_hooks([message.author.id, 'PRIVMSG', {'target': target, 'text': message.content}])

class DiscordServer(ClientbotBaseProtocol):
    S2S_BUFSIZE = 0

    def __init__(self, name, parent, server_id):
        conf.conf['servers'][name] = {}
        super().__init__(name)
        self.virtual_parent = parent
        self.sidgen = PUIDGenerator('DiscordInternalSID')
        self.uidgen = PUIDGenerator('PUID')
        self.sid = server_id
        self.servers[self.sid] = Server(self, None, '0.0.0.0', internal=False, desc=name)

    def _init_vars(self):
        super()._init_vars()
        self.casemapping = 'ascii'  # TODO: investigate utf-8 support
        self.cmodes = {'op': 'o', 'halfop': 'h', 'voice': 'v', 'owner': 'q', 'admin': 'a',
                       '*A': '', '*B': '', '*C': '', '*D': ''}
        # The actual prefix chars don't matter; we just need to make sure +qaohv are in
        # the prefixmodes list so that the mode internals work properly
        self.prefixmodes = {'q': '~', 'a': '&', 'o': '@', 'h': '%', 'v': '+'}

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
            discord_target = self.users[target].discord_user.user.open_dm()
        else:
            discord_target = self.channels[target].discord_channel

        message_data = {'target': discord_target, 'sender': source}
        if self.pseudoclient and self.pseudoclient.uid == source:
            message_data['text'] = I2DFormatter().format(text)
            self.virtual_parent.message_queue.put_nowait(message_data)
            return

        self.call_hooks([source, 'CLIENTBOT_MESSAGE', {'target': target, 'is_notice': notice, 'text': text}])

    def join(self, client, channel):
        """STUB: Joins a user to a channel."""
        self._channels[channel].users.add(client)
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
        # Treat Discord channel IDs and names both as channels
        return s in self.bot_plugin.state.channels or str(s).startswith('#')

    def is_server_name(self, s):
        """Returns whether the string given is a valid IRC server name."""
        return s in self.bot_plugin.state.guilds

    def get_friendly_name(self, entityid, caller=None):
        """
        Returns the friendly name of a SID (the guild name), UID (the nick), or channel (the name).
        """
        # IRC-style channel link, return as is
        if isinstance(entityid, str) and entityid.startswith('#'):
            return entityid
        # internal PUID, handle appropriately
        elif isinstance(entityid, str) and '@' in entityid:
            if entityid in self.users:
                return self.users[entityid].nick
            elif caller and entityid in caller.users:
                return caller.users[entityid].nick
            else:
                # Probably a server
                return entityid.split('@', 1)[0]

        if self.is_channel(entityid):
            return '#' + self.bot_plugin.state.channels[entityid].name
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

    def _create_child(self, name, server_id):
        """
        Creates a virtual network object for a server with the given name.
        """
        if name in world.networkobjects:
            raise ValueError("Attempting to reintroduce network with name %r" % name)
        child = DiscordServer(name, self, server_id)
        world.networkobjects[name] = self._children[name] = child
        return child

    def _remove_child(self, name):
        """
        Removes a virtual network object with the given name.
        """
        pylink_netobj = self._children[name]
        pylink_netobj.call_hooks([None, 'PYLINK_DISCONNECT', {}])
        del self._children[name]
        del world.networkobjects[name]

    def connect(self):
        self._aborted.clear()
        self.client.run()

    def disconnect(self):
        """Handles disconnections from Discord."""
        self._aborted.set()

        self._pre_disconnect()

        children = self._children.copy()
        for child in children:
            self._remove_child(child)

        self.client.gw.shutting_down = True
        self.client.gw.ws.close()

        self._post_disconnect()

Class = PyLinkDiscordProtocol
