# Text-only Telegram NFT offer bot

The bot posts **native Telegram text**, HTML-formatted links, and inline buttons. It does not generate or send an image card. The countdown in pending offers is refreshed every 60 seconds by editing the bot's own message.

## Install
Python 3.11+
```bash
python -m venv .venv
# Windows: .venv\Scripts\activate
# macOS/Linux: source .venv/bin/activate
pip install -r requirements.txt
```
Copy `.env.example` to `.env`, set `BOT_TOKEN` and your numeric Telegram ID in `ADMIN_IDS`, then:
```bash
python bot.py
```

## Group setup
Create a bot with `@BotFather`, add it to the group, and disable Group Privacy in BotFather if it must read ordinary messages containing a gift link and amount. The bot sends its own offer message because Telegram bots cannot edit a message authored by a regular user.

Example:
`https://t.me/nft/InstantRamen-181091 444 stars`

## Commands
- `/admin`
- `/setrecipient username`
- `/addadmin user_id` (only IDs in ADMIN_IDS)
- `/deladmin user_id` (only IDs in ADMIN_IDS)

## Custom emoji packs
A pack link such as `https://t.me/addemoji/CryptoBotAssets` does not reveal the custom emoji IDs needed by the Bot API. To use those exact premium/custom emoji, obtain each `custom_emoji_id` from a message containing that emoji and implement Telegram `MessageEntity(type="custom_emoji", custom_emoji_id=...)` entities. Standard ⭐ is used until those IDs are configured.

## Real transfer verification
The verifier deliberately fails closed. Telegram Bot API has no general endpoint to independently verify gift ownership transfers for arbitrary accounts, so a provider-specific authorized integration is required before a deal can be marked complete.
