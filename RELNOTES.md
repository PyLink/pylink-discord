# [pylink-discord 0.2.0](https://github.com/PyLink/pylink-discord/releases/tag/0.2.0)

Second alpha release, targeting PyLink 2.1-alpha2 and Disco Git master.

### Feature changes
- Added a `join_offline_users` option to show/hide offline Discord users from channels (#15)
- Rewritten webhooks engine: webhooks support is now more robust against missing permissions, webhook deletion, etc.
- Removed Webhooks agent forwarding
- Added a `pm_format` option to control the display style of relayed PMs

### Bug fixes
- Messages are now wrapped and truncated if over Discord's length limit (2000 characters)
- Reworked DM handling to properly support multiple guilds
- Set infinite reconnect attempts to Discord
- Fix extraneous JOIN / MODE hooks after the bot is removed from a channel

### Internal improvements
- Implement nick() to change the bot's nick
- Declare `freeform-nicks`, `virtual-server` protocol capabilities


# [pylink-discord 0.1.0](https://github.com/PyLink/pylink-discord/releases/tag/0.1.0)

Initial release targeting PyLink 2.1-dev:

Working so far:
- Relaying messages & user lists to IRC
- Relaying messages to Discord as text or webhooks
- State tracking: channel, presence updates
- Syncing Discord permissions (one-way) as IRC modes, even as user, channel settings change
- Normalizing Unicode nicks to IRC ASCII (2.1 relay feature)
- Basic formatting works mostly OK (bold, underline, italic)
- Attachments sent to Discord are relayed as links to IRC

