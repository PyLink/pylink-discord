# pylink-discord

An alpha module linking [PyLink](https://github.com/jlu5/PyLink) to Discord!

## Requirements
- CPython 3.5 or later
- PyLink 2.1-dev (currently in the [*`devel`*](https://github.com/jlu5/PyLink/tree/devel) branch in git
- [jlu5/disco](https://github.com/jlu5/disco) - fork of the [Disco](https://github.com/b1naryth1ef/disco) Discord API library with some permissions fixes.

You can also install these dependencies via pip (for Python 3) using: `pip3 install -r requirements.txt`

## Setup

1) Install the requirements listed above. You MUST use the versions listed above, or things will not work!
2) Clone this repository somewhere: `git clone https://github.com/pylink/pylink-discord`
2) Set up a Discord application + bot user. https://github.com/reactiflux/discord-irc/wiki/Creating-a-discord-bot-&-getting-a-token is a solid guide on how to do so.
3) Add the protocols folder in this repository to [`protocol_dirs` in your PyLink config](https://github.com/jlu5/PyLink/blob/ba17821/example-conf.yml#L57-L60).
4) Configure a server block using the `discord` protocol module:

```
    discord-ctrl:
        token: "your.discord.token.keep.this.private!"
        netname: "Discord"
        protocol: discord
        # This maps guild IDs to PyLink network names, so that they have a consistent name for
        # plugins like Relay. You can more easily view IDs by turning on Developer Mode in
        # Discord: https://support.discordapp.com/hc/en-us/articles/206346498-Where-can-I-find-my-User-Server-Message-ID-
        # You SHOULD set a name for every guild your bot is in, or it will fall back to guild IDs (which are not pretty!)
        server_names:
            # overdrive networks (our magical server!)
            497939890063802369: ovddsc
            # another example
            123456789000000000: chatutopia

        # Sets whether we should burst Discord guild owners as IRC owners
        show_owner_status: true

```

5) Start PyLink using the `pylink-discord` wrapper in the repository root. This is **important** as this wrapper applies gevent patching, which is required by the underlying disco library.

## Usage

Channels, guilds, and users are all represented as IDs in Discord and thus in this protocol module too.
This affects plugins: e.g. in Relay you must use command forms like `link masternet #channel 12345678`, where 12345678 is your Discord channel ID.

Other than that, Relay should work on Discord much like it does on IRC Clientbot. On full server links, Discord users are bursted as virtual IRC users.

Private channels are supported too - just add access so that the bot can read it!

## Implementation Notes & Limitations

- Each Discord guild that the bot is in is represented as a separate PyLink network. This means that per-guild nicks work, and are tracked across nick changes!
- Discord does not have a rigid concept of a channel's user list. So, we instead check permissions on each guild member, and consider someone to be "in" a channel if they have the Read Messages permission there.
- Starting in PyLink 2.1-dev, Unicode nicks are translated to IRC ASCII by Relay when not supported by the receiving IRCd. Installing [unidecode](https://github.com/avian2/unidecode) will allow PyLink Relay to do a best effort transliteration of Unicode characters to ASCII, instead of replacing all unrecognized characters with `-`.
- Kicks, modes, and most forms of IRC moderation are **not supported**, as it is way out of our scope to bidirectionally sync IRC modes (which are complicated!) and Discord permissions (which are also complicated!).
    - Attempts to kick from IRC are bounced because there is no equivalent concept on Discord (Discord kicks are by guild).
- Permissions from Discord channels are synced to IRC (and hopefully updated live through permission and role changes):
    - IRC admin (+a) corresponds to the Administrator permission
    - IRC op (+o) corresponds to the Manage Messages permission
    - IRC halfop (+h) corresponds to Kick Members
    - IRC voice (+v) corresponds to Send Messages
- Basic formatting works mostly OK (bold, underline, italic).
- Attachments and embeds on Discord are relayed as text to IRC.
