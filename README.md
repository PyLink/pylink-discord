# pylink-discord

**THIS MODULE WAS DISCONTINUED IN 2020-10 AND IS ONLY PROVIDED HERE FOR HISTORICAL REFERENCE. PLEASE DO NOT ASK ME FOR SUPPORT**

## Configuration reference

```yaml
servers:
    discord-ctrl:
        token: "your.discord.token.keep.this.private!"
        netname: "Discord"
        protocol: discord
        # Define a list of guilds (servers) and their settings.
        guilds:
            # Set this to the ID of your server - you can find it by enabling Developer Mode and right clicking
            # your server's icon in the server list
            101010101010101010:
                # The name to use for this guild in PyLink. This should be different from any other networks you've specified
                # in your config. If you're only relaying to one guild, setting this to "discord" is a pretty safe bet.
                name: yournet-dsc

                # Toggles username spoofing via webhooks for a more transparent relay experience. This requires the bot to have
                # the Manage Webhooks permission, and defaults to false if not set.
                use_webhooks: false

                # If disabled, users that are marked as "Invisible" or "Offline" will not be joined to
                # linked channels until they come online. This can be useful if you have many offline users compared
                # to online ones. Note that if this is disabled, PMs cannot be sent to offline Discord users
                # Changes to this setting require a restart to apply. This defaults to true if not set.
                join_offline_users: true

                # Optional: map a list of roles to IRC modes. You can find role IDs by enabling Developer Mode
                # and right clicking a role in the user info pane or Roles configuration page.
                role_mode_map:
                    123456789012345678: op
                    001122334455667788: voice

            # Another example
            123456789000000000:
                name: chatutopia
                use_webhooks: true

        # Sets whether we should show Discord guild owners as IRC owners
        show_owner_status: true

        # Sets the format for usernames when using webhooks: supported fields include user fields
        # ($nick, $ident, $host, etc.) as well as the network name ($netname) and short network tag ($nettag)
        webhook_user_format: "$nick @ IRC/$netname"

        # Sets the format used when relaying PMs from IRC to Discord. The same fields as webhook_user_format
        # apply, plus $text (the contents of the message).
        pm_format: "Message from $nick @ $netname: $text"

        # Here you can set how edited channel messages are formatted. Edited messages are re-broadcasted
        # as a separate message, in terms of Relay and PyLink commands. If this is empty, edited messages will
        # be sent without any additional changes.
        #editmsg_format: "\x02Edit:\x02 %s"

        # Determines whether the bot will send @here and @everyone when relaying messages. This is disabled by
        # default to prevent people from spamming these triggers fairly easily.
        #allow_mention_everyone: false

        # You can associate IRC services accounts with preferred avatar URLs. Currently this is
        # quite limited and requires hardcoding things in the config; eventually there may be
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
        #default_avatar_url: ""

```

## Implementation details

- Channels, guilds, and users are all represented internally using Discord IDs.
- Private channels are supported too - just add access so that the bot can read it!
- Each Discord guild that the bot is in is represented as a separate PyLink network. This means that per-guild nicks work, and are tracked across nick changes
- Discord does not have a rigid concept of a channel's user list. So, we instead check permissions on each guild member, and consider someone to be "in" a channel if they have the Read Messages permission there.
- Starting in PyLink 2.1, Unicode nicks are translated to IRC ASCII by Relay when not supported by the receiving IRCd. Installing [unidecode](https://github.com/avian2/unidecode) will allow PyLink Relay to do a best effort transliteration of Unicode characters to ASCII, instead of replacing all unrecognized characters with `-`.
- Kicks, modes, and most forms of IRC moderation are **not supported**, as it is way out of our scope to bidirectionally sync IRC modes (which are complicated!) and Discord permissions (which are also complicated!).
    - Attempts to kick from IRC are bounced because there is no equivalent concept on Discord (Discord kicks are by guild).
- Attachments sent to Discord are relayed as a link to IRC.

