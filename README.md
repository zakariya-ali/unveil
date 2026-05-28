# Unveil

A desktop app that analyzes every player in your current Valorant match and flags suspicious accounts in real time.

## What it does

While you are in agent select or in a live match, Unveil fetches the last 10 competitive games for every player in the lobby and computes their KD ratio, headshot percentage, win rate, and longest gap between play sessions. It then labels each player with one or more tags:

| Tag | Condition |
|-----|-----------|
| cheater? | KD 1.5 or higher |
| smurf? | KD 1.3 to 1.5 |
| normal | KD 0.8 to 1.3 |
| weak | KD below 0.8 |
| bought? | gap of 60+ days between matches (suggests account purchase) |
| HIGH HS | headshot rate above 30% |
| SUS WR | win rate above 60% |
| new/no-data | fewer than 3 analyzable matches found |

Players using incognito mode are unmasked via the HenrikDev API when possible. Their names appear highlighted in the UI. Players that cannot be unmasked are shown as hidden with a partial ID.

Each player row displays their agent icon, current rank icon, in-game name, subline with agent and rank text, and a stats cluster showing KD, HS%, WR, and match count.

## Requirements

- Windows 10 or 11
- Python 3.10 or later
- A HenrikDev API key (free tier works, see Configuration below)

## Installation

```
pip install -r requirements.txt
```

## Running

```
python valorant_gui.py
```

## Usage

### 1PC mode (default)

Use this if Valorant is running on the same PC as Unveil. No login is needed. The app reads Riot's lockfile automatically.

1. Launch Valorant and get into a match or agent select.
2. Run Unveil.
3. Click **Analyze current match**.

### 2PC mode

Use this if Valorant is running on a different machine, such as a gaming PC, while Unveil runs on a second machine.

1. Click the **1PC (local)** button in the top bar.
2. Select **Login via RSO (2PC)**.
3. A browser tab opens to the Riot login page. Log in with your Riot account.
4. After login, you will be redirected to a page that shows a 404 error. This is expected. Copy the full URL from the browser's address bar.
5. Paste the URL into the dialog and click **Submit**.
6. If your region cannot be detected automatically, select it from the list.

Once logged in, click **Analyze current match** as normal. The app will reach out to Riot's servers on behalf of your account.

### Rate limits

The HenrikDev API has a request budget. The remaining budget is shown in the status bar after each analysis. If the budget runs out, the Analyze button shows a countdown and re-enables automatically when the cooldown clears.

## Configuration

The HenrikDev API key is set at the top of `valorant_match_info.py`:

```python
return os.environ.get('HENRIK_API_KEY', 'your-key-here')
```

You can override it without editing the file by setting the environment variable:

```
set HENRIK_API_KEY=your-key-here
python valorant_gui.py
```

Get a free key at [https://docs.henrikdev.xyz](https://docs.henrikdev.xyz).

## Notes

- Analysis only covers competitive queue matches.
- Flags are heuristic. They indicate statistical outliers, not confirmed cheating or smurfing.
- The app does not modify any game files and does not interact with the game client beyond reading the lockfile.

