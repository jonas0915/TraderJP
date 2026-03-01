# TraderJP — Manual Setup Tasks

Complete these steps in order before running the bot.

---

## Step 1 — Fix Your Tradovate Login in .env

Open the file: `/home/user/TraderJP/.env`

Change these two lines:

```
TRADOVATE_USERNAME=you@example.com       ← your actual email address
TRADOVATE_PASSWORD=your_actual_password  ← the password you use on trader.tradovate.com
```

**Important:**
- The username MUST be your email address — NOT your Apex account ID (APEX_261113 is wrong)
- Use the exact same email and password you use to log into https://trader.tradovate.com

---

## Step 2 — Confirm Your Tradovate Account Name

After logging in at https://trader.tradovate.com, check the account name shown in the top-left dropdown.

In your `.env`:
```
TRADOVATE_ACCOUNT_NAME=PA-APEX-261113-50
```

Make sure this matches exactly what Tradovate shows (including dashes and casing).
If it doesn't match, update it. Leave it blank to auto-select the first account.

---

## Step 3 — Verify the Demo vs Live Setting

Your `.env` currently has:
```
TRADOVATE_LIVE=false
```

**Apex accounts always use Tradovate's DEMO servers**, even for funded/PA accounts.
Keep `TRADOVATE_LIVE=false` unless you have a direct (non-Apex) Tradovate live account.

---

## Step 4 — Test Your Login Manually (Recommended)

Before running the bot, confirm your credentials work by visiting:
https://trader.tradovate.com

Log in with the same email/password you put in `.env`.
If you can't log in on the website, the bot won't work either.

If you forgot your password, reset it at:
https://trader.tradovate.com (click "Forgot password")

---

## Step 5 — Run the Bot

Once the above steps are done:

```bash
cd /home/user/TraderJP
python3 main.py
```

The bot will now give a clear error message if the email format is wrong,
so you'll know immediately if Step 1 was missed.

---

## Troubleshooting

| Error | Cause | Fix |
|-------|-------|-----|
| `does not look like an email address` | Username is not an email | Fix Step 1 |
| `Incorrect username or password` | Wrong email or password | Fix Step 1, verify on website |
| `Account 'X' not found` | Account name mismatch | Fix Step 2 |
| `No accounts found` | Wrong login or account not linked | Contact Apex support |

---

## Summary of What Was Already Fixed in the Code

- `main.py` — Bot now exits immediately with a clear message if the username is not an email address
- `config/.env.example` — Updated to clearly state that an email address is required
