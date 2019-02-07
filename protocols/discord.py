import calendar
import operator
from collections import defaultdict
from functools import reduce

import websocket
from disco.bot import Bot, BotConfig
from disco.bot import Plugin
from disco.client import Client, ClientConfig
from disco.gateway.events import GuildCreate, ChannelCreate, MessageCreate
from disco.types import Guild, Channel as DiscordChannel, GuildMember, Message
from disco.types.channel import ChannelType
from disco.types.permissions import Permissions
from disco.util.logging import setup_logging
from holster.emitter import Priority
from pylinkirc.classes import *
from pylinkirc.log import log
from pylinkirc.protocols.clientbot import ClientbotWrapperProtocol

from ._discord_formatter import I2DFormatter

websocket.enableTrace(True)

class DiscordBotPlugin(Plugin):
    subserver = {}
    irc_discord_perm_mapping = {
        'voice': Permissions.SEND_MESSAGES.value,
        'halfop': Permissions.KICK_MEMBERS.value,
        'op': Permissions.BAN_MEMBERS.value,
        'admin': Permissions.ADMINISTRATOR.value
    }
    ALL_PERMS = reduce(operator.ior, Permissions.values_)
    botuser = None

    def __init__(self, protocol, bot, config):
        self.protocol = protocol
        super().__init__(bot, config)

    @Plugin.listen('Ready')
    def on_ready(self, event, *args, **kwargs):
        self.client.gw.ws.emitter.on('on_close', self.protocol.websocket_close, priority=Priority.BEFORE)
        self.botuser = str(event.user.id)

    @Plugin.listen('GuildCreate')
    def on_server_connect(self, event: GuildCreate, *args, **kwargs):
        server: Guild = event.guild
        pylink_netobj: DiscordServer = self.protocol._create_child(server.name, server.id)
        pylink_netobj.uplink = server.id
        member: GuildMember
        for member_id, member in server.members.items():
            uid = str(member.id)
            user = User(pylink_netobj, member.user.username, calendar.timegm(member.joined_at.timetuple()), uid, str(server.id))
            user.discord_user = member
            pylink_netobj.users[uid] = user
            if uid == self.botuser:
                pylink_netobj.pseudoclient = user
            self.protocol._add_hook(
                server.name, [
                    server.id,
                    'UID',
                    {
                        'uid': uid,
                        'ts': user.ts,
                        'nick': user.nick,
                        'realhost': user.realhost,
                        'host': user.host,
                        'ident': user.ident,
                        'ip': user.ip
                    }])
            user.permissions = self.compute_base_permissions(member, server)

        channel: DiscordChannel
        for channel_id, channel in server.channels.items():
            if channel.type == ChannelType.GUILD_TEXT:
                namelist = []
                chandata = pylink_netobj.channels[str(channel)] = Channel(pylink_netobj, name=str(channel))
                channel_modes = set()
                for uid, user in pylink_netobj.users.items():
                    discord_user = server.members[int(uid)]
                    channel_permissions = self.compute_user_channel_perms(user.permissions, discord_user, channel)
                    if channel_permissions & Permissions.READ_MESSAGES.value == Permissions.READ_MESSAGES.value:
                        namelist.append(uid)
                        pylink_netobj.users[uid].channels.add(str(channel))
                        pylink_netobj.channels[str(channel)].users.add(uid)
                        for irc_mode, discord_permission in self.irc_discord_perm_mapping.items():
                            if channel_permissions & discord_permission == discord_permission:
                                channel_modes.add(('+%s' % pylink_netobj.cmodes[irc_mode], uid))
                pylink_netobj.apply_modes(str(channel), channel_modes)
                chandata.discord_channel = channel
                self.protocol._add_hook(
                    server.name, [
                        server.id,
                        'JOIN',
                        {
                            'channel': str(channel),
                            'users': namelist,
                            'modes': [],
                            'ts': chandata.ts,
                            'channeldata': chandata
                        }])


        self.subserver[server.name] = pylink_netobj
        pylink_netobj.connected.set()
        self.protocol._add_hook(server.name, [server.id, 'ENDBURST', {}])

    @Plugin.listen('ChannelCreate')
    def on_channel_create(self, event: ChannelCreate, *args, **kwargs):
        pass

    def compute_base_permissions(self, member, guild):
        if guild.owner == member:
            return self.ALL_PERMS

        # get @everyone role
        role_everyone = guild.roles[guild.id]
        permissions = role_everyone.permissions.value

        for role in member.roles:
            permissions |= guild.roles[role].permissions.value

        if permissions & Permissions.ADMINISTRATOR.value == Permissions.ADMINISTRATOR.value:
            return self.ALL_PERMS

        return permissions

    def compute_user_channel_perms(self, base_permissions, member, channel):
        # ADMINISTRATOR overrides any potential permission overwrites, so there is nothing to do here.
        if base_permissions & Permissions.ADMINISTRATOR.value == Permissions.ADMINISTRATOR.value:
            return self.ALL_PERMS

        permissions = base_permissions
        # Find (@everyone) role overwrite and apply it.
        overwrite_everyone = channel.overwrites.get(channel.guild_id)
        if overwrite_everyone:
            permissions &= ~overwrite_everyone.deny.value
            permissions |= overwrite_everyone.allow.value

        # Apply role specific overwrites.
        overwrites = channel.overwrites
        allow = 0
        deny = 0
        for role_id in member.roles:
            overwrite_role = overwrites.get(role_id)
            if overwrite_role:
                allow |= overwrite_role.allow.value
                deny |= overwrite_role.deny.value

        permissions &= ~deny
        permissions |= allow

        # Apply member specific overwrite if it exist.
        overwrite_member = overwrites.get(member.id)
        if overwrite_member:
            permissions &= ~overwrite_member.deny
            permissions |= overwrite_member.allow

        return permissions

    @Plugin.listen('MessageCreate')
    def on_message(self, event: MessageCreate, *args, **kwargs):
        message: Message = event.message
        subserver = None
        target = None

        # If the bot is the one sending the message, don't do anything
        if str(message.author.id) == self.botuser or message.webhook_id:
            return

        if not message.guild:
            # This is a DM
            # see if we've seen this user on any of our servers
            for server in self.subserver.values():
                if str(message.author.id) in server.users:
                    target = self.botuser
                    subserver = server.name
                    server.users[str(message.author.id)].dm_channel = str(message.channel.id)
                    server.channels[str(message.channel)] = Channel(server, name=str(message.channel))
                    server.channels[str(message.channel)].discord_channel = message.channel

                    break
            if not (subserver or target):
                return
        else:
            subserver = message.guild.name
            target = message.channel

        self.protocol._add_hook(
            subserver,
            [str(message.author.id), 'PRIVMSG', {'target': str(target), 'text': message.content}]
        )


class DiscordServer(ClientbotWrapperProtocol):
    def __init__(self, name, parent, server_id):
        conf.conf['servers'][name] = {}
        super().__init__(name)
        self.virtual_parent = parent
        self.sidgen = PUIDGenerator('DiscordInternalSID')
        self.uidgen = PUIDGenerator('PUID')
        self.sid = str(server_id)
        self.servers[self.sid] = Server(self, None, '0.0.0.0', internal=False, desc=name)

    def _init_vars(self):
        super()._init_vars()
        self.casemapping = 'ascii'  # TODO: investigate utf-8 support
        self.cmodes = {'op': 'o', 'halfop': 'h', 'voice': 'v', 'owner': 'q', 'admin': 'a',
                       '*A': '', '*B': '', '*C': '', '*D': ''}


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

        if not self.is_channel(target):
            self.call_hooks([source, 'CLIENTBOT_MESSAGE', {'target': target, 'is_notice': notice, 'text': text}])
            return

        try:
            text = I2DFormatter().format(text)
            remotenet, remoteuser = self.users[source].remote
            channel_webhooks = discord_target.get_webhooks()
            if channel_webhooks:
                message_data['webhook'] = channel_webhooks[0]
                message_data.update(self.get_user_webhook_data(remoteuser, remotenet))
            message_data['text'] = I2DFormatter().format(text)
            self.virtual_parent.message_queue.put_nowait(message_data)
        except (AttributeError, KeyError):
            self.call_hooks([source, 'CLIENTBOT_MESSAGE', {'target': target, 'is_notice': notice, 'text': text}])

    def join(self, client, channel):
        """STUB: Joins a user to a channel."""
        self._channels[channel].users.add(client)
        self.users[client].channels.add(channel)

        log.debug('(%s) join: faking JOIN of client %s/%s to %s', self.name, client,
                  self.get_friendly_name(client), channel)
        self.call_hooks([client, 'CLIENTBOT_JOIN', {'channel': channel}])

    def send(self, data, queue=True):
        pass

    def get_user_webhook_data(self, uid, network):
        user = world.networkobjects[network].users[uid]
        return {
            'username': "{} (IRC @ {})".format(user.nick, network)
        }


class PyLinkDiscordProtocol(PyLinkNetworkCoreWithUtils):

    def __init__(self, *args, **kwargs):
        from gevent import monkey
        monkey.patch_all()
        super().__init__(*args, **kwargs)
        self._hooks_queue = queue.Queue()

        if 'token' not in self.serverdata:
            raise ProtocolError("No API token defined under server settings")
        self.client_config = ClientConfig({'token': self.serverdata['token']})
        self.client = Client(self.client_config)
        self.bot_config = BotConfig()
        self.bot = Bot(self.client, self.bot_config)
        self.bot_plugin = DiscordBotPlugin(self, self.bot, self.bot_config)
        self.bot.add_plugin(self.bot_plugin)
        setup_logging(level='DEBUG')
        self._children = {}
        self.message_queue = queue.Queue()

    def _message_builder(self):
        current_channel_senders = {}
        joined_messages = defaultdict(dict)
        while not self._aborted.is_set():
            try:
                message = self.message_queue.get(timeout=0.1)
                message_text = message.pop('text', '')
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
                joined_messages = defaultdict(dict)
                current_channel_senders = {}

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


    def _process_hooks(self):
        """Loop to process incoming hook data."""
        while not self._aborted.is_set():
            data = self._hooks_queue.get()
            if data is None:
                log.debug('(%s) Stopping queue thread due to getting None as item', self.name)
                break
            elif self not in world.networkobjects.values():
                log.debug('(%s) Stopping stale queue thread; no longer matches world.networkobjects', self.name)
                break

            subserver, data = data
            if subserver not in world.networkobjects:
                log.error('(%s) Not queuing hook for subserver %r no longer in networks list.',
                          self.name, subserver)
            elif subserver in self._children:
                self._children[subserver].call_hooks(data)

    def _add_hook(self, subserver, data):
        """
        Pushes a hook payload for the given subserver.
        """
        if subserver not in self._children:
            raise ValueError("Unknown subserver %s" % subserver)
        self._hooks_queue.put_nowait((
            subserver,
            data
        ))

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
        self._add_hook(name, [None, 'PYLINK_DISCONNECT', {}])
        del self._children[name]
        del world.networkobjects[name]

    def connect(self):
        self._aborted.clear()

        self._queue_thread = threading.Thread(name="Queue thread for %s" % self.name,
                                             target=self._process_hooks, daemon=True)
        self._queue_thread.start()

        self._message_thread = threading.Thread(name="Message thread for %s" % self.name,
                                                target=self._message_builder, daemon=True)
        self._message_thread.start()

        self.client.run()

    def websocket_close(self, *_, **__):
        return self.disconnect()

    def disconnect(self):
        self._aborted.set()

        self._pre_disconnect()

        log.debug('(%s) Killing hooks handler', self.name)
        try:
            # XXX: queue.Queue.queue isn't actually documented, so this is probably not reliable in the long run.
            with self._hooks_queue.mutex:
                self._hooks_queue.queue[0] = None
        except IndexError:
            self._hooks_queue.put(None)

        children = self._children.copy()
        for child in children:
            self._remove_child(child)

        if world.shutting_down.is_set():
            self.bot.client.gw.shutting_down = True
        log.debug('(%s) Sending Discord logout', self.name)
        self.bot.client.gw.session_id = None
        self.bot.client.gw.ws.close()

        self._post_disconnect()

Class = PyLinkDiscordProtocol
