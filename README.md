# Advanced OTT Refer Telegram Bot

Modern referral Telegram bot with:
- Force-subscription (admin-managed channels)
- Temp invite URLs per channel button
- Referral points + leaderboard
- OTT-wise withdrawal sections (Netflix, Crunchyroll, etc.)
- Reward stock + reward login/pass storage per reward
- Multi image/video `/start` media
- Heroku support

## Commands
### User
- `/start [ref_xxx]`

### Admin (OWNER_ID)
- `/addchannel -1001234567890 MainChannel`
- `/addreward <OTT> <RewardName> <Stock> <CostPoints> <LoginID> <Password>`

Example:
- `/addreward Netflix Premium 10 250 email@example.com MyPass123`

## Environment
See `.env.example`.

## Run local
```bash
pip install -r requirements.txt
python bot.py
```

## Heroku
- `Procfile`, `runtime.txt`, `app.json` already included.
- Set env vars in Heroku config.
