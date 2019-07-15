# pylink-discord

**pylink-discord** provides Discord integration for [PyLink](https://github.com/jlu5/PyLink). Using this module, you can transparently relay conversations between IRC and Discord (using virtual clients on IRC & webhook users on Discord), as well as between multiple Discord guilds.

## Requirements
- CPython 3.5 or later
- PyLink 2.1-alpha2+ (currently in the [**`devel`**](https://github.com/jlu5/PyLink/tree/devel) branch in git)
- [disco](https://github.com/b1naryth1ef/disco) (git master, commit [7fbca825](https://github.com/b1naryth1ef/disco/commit/7fbca825f85a0936487d0b780dc53dcdbb920e21) or later) - Discord API library
- *Optional:* [libgravatar](https://github.com/pabluk/libgravatar) - provides Gravatar support when configuring avatars

You can also install these dependencies via pip (for Python 3) using: `pip3 install -r requirements.txt`

## Setup

1) Install the requirements listed above. You MUST use the versions listed above, or things will not work!
2) Clone this repository somewhere: `git clone https://github.com/pylink/pylink-discord`
3) Set up a Discord application + bot user. https://github.com/reactiflux/discord-irc/wiki/Creating-a-discord-bot-&-getting-a-token is a solid guide on how to do so.
4) Add the protocols folder in this repository to the [`protocol_dirs`](https://github.com/jlu5/PyLink/blob/ba17821/example-conf.yml#L57-L60) option in your PyLink config.
5) Configure a server block using the `discord` protocol module:

```yaml

    discord-ctrl:
        token: "your.discord.token.keep.this.private!"
        netname: "Discord"
        protocol: discord

        # This config block uses guild IDs, so that settings and (PyLink) network names are consistent
        # across guild renames. You can easily find IDs by turning on Developer Mode in Discord:
        # https://support.discordapp.com/hc/en-us/articles/206346498-Where-can-I-find-my-User-Server-Message-ID-

        # You SHOULD set a name for every guild your bot is in, or the protocol module will fall back
        # to guild IDs as PyLink network names (which are not pretty!)
        guilds:
            # overdrive networks (our magical server!)
            497939890063802369:
                name: ovddsc
                use_webhooks: false
                # If disabled, users that are marked as "Invisible" or "Offline" will not be joined to
                # linked channels until they come online. Useful if you have many offline users compared
                # to online ones. Note that if this is disabled, PMs cannot be sent to offline Discord users
                # Changes to this setting require a reconnect to apply. This defaults to true if not set.
                join_offline_users: true
            # another example
            123456789000000000:
                name: chatutopia
                use_webhooks: true

        # Sets whether we should burst Discord guild owners as IRC owners
        show_owner_status: true

        # Sets the format for usernames when using webhooks: supported fields include user fields
        # ($nick, $ident, $host, etc.) as well as the network name ($netname) and short network tag ($nettag)
        webhook_user_format: "$nick @ IRC/$netname"

        # Sets the format used when relaying PMs from IRC to Discord. The same fields as webhook_user_format
        # apply, plus $text (the contents of the message).
        pm_format: "Message from $nick @ $netname: $text"

        # You can associate IRC services accounts with preferred avatar URLs. Currently this is
        # quite limited and requires hardcoding things in the config; eventually there will be
        # a self-service process to do this.
        #
        # Gravatar emails in the form "gravatar:some@email.com" are supported if libgravatar is installed.
        # http:// and https:// URLs also work.
        avatars:
            user1: "gravatar:user1@example.com"
            abcd: "https://abcd.example.com/avatar.png"

        # For users without an avatar set, you can use a default avatar URL (http or https).
        # If this is unset, the bot will just use the default webhook icon (which you can customize
        # per channel if desired).
        default_avatar_url: "https://ircnet.overdrivenetworks.com/img/relaypic.png"

```

6) Start PyLink using the `pylink-discord` wrapper in the repository root. This is **important** as this wrapper applies gevent patching, which is required by the underlying disco library.


## Permissions

pylink-discord needs the following permissions to work:

- **Read Messages** and **Send Messages** on all channels you plan to use Relay on.
- **Manage Webhooks** if you intend to use webhooks mode. pylink-discord creates and manages its own set of webhooks (one per channel) to simplify configuration.
- *Optional:* **Change Nickname** - allows you to modify the bot's nick via the `nick` command in PyLink.

## Usage

Channels, guilds, and users are all represented as IDs in Discord and thus in this protocol module too.
This affects plugins: e.g. in Relay you must use commands like `link masternet #channel 12345678`, where 12345678 is your Discord channel ID.

Other than that, Relay should work on Discord much like it does on IRC Clientbot. On full server links, Discord users are bursted as virtual IRC users.

Private channels are supported too - just add access so that the bot can read it!


## Implementation Notes & Limitations

- Each Discord guild that the bot is in is represented as a separate PyLink network. This means that **per-guild nicks work**, and are tracked across nick changes!
- Discord does not have a rigid concept of a channel's user list. So, we instead check permissions on each guild member, and consider someone to be "in" a channel if they have the Read Messages permission there.
- Starting in PyLink 2.1, Unicode nicks are translated to IRC ASCII by Relay when not supported by the receiving IRCd. Installing [unidecode](https://github.com/avian2/unidecode) will allow PyLink Relay to do a best effort transliteration of Unicode characters to ASCII, instead of replacing all unrecognized characters with `-`.
- Kicks, modes, and most forms of IRC moderation are **not supported**, as it is way out of our scope to bidirectionally sync IRC modes (which are complicated!) and Discord permissions (which are also complicated!).
    - Attempts to kick from IRC are bounced because there is no equivalent concept on Discord (Discord kicks are by guild).
- Permissions from Discord channels are synced to IRC (and hopefully updated live through permission and role changes):
    - IRC admin (+a) corresponds to the Administrator permission
    - IRC op (+o) corresponds to the Manage Messages permission
    - IRC halfop (+h) corresponds to Kick Members
    - IRC voice (+v) corresponds to Send Messages
- Basic formatting works mostly OK (bold, underline, italic).
- Attachments sent to Discord are relayed as a link to IRC.

## TODO

https://github.com/PyLink/pylink-discord/issues
