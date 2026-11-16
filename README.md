# herdr-devin-limits

A thin strip at the bottom of the [Herdr](https://herdr.dev) desktop app that shows
your Devin CLI Pro plan's daily and weekly quota, as bars, percent used and the time
left until each window resets. It also shows the current overage balance.

![devin-limits](preview.png)

```
<logo> ━━━━━ 37% 4h 27m · ━━━━━ 18% 2d 2h · $7.08
```

- The bar and percent turn the theme's yellow from 80% and red from 95%.
- Time left reads `48m`, `4h 48m` or `2d 2h`.
- Overage is shown in dollars to two decimal places.
- It refreshes every 5 minutes and about 15 s after a window resets. Click the strip
  to refresh now: it dims until the refresh ends, and shows `· update failed` if it
  fails.
- If a refresh fails, the last values stay, dimmed, with `· stale 12m`; it retries
  after 1 minute, backing off to 5 minutes.
- Missing or rejected credentials show the logo and `not signed in`; an account not
  billed by quota (ACU or enterprise billing) shows `no quota`. Other failures show
  `unavailable`.

## Requirements

- The Herdr desktop app
- A Devin CLI Pro plan with a valid API key in the Devin credentials TOML
- `python3` 3.11 or newer (standard library only)

## Install

```sh
git clone <this repo> && cd herdr-devin-limits
./install.sh            # copies mods/devin-limits into ~/.config/herdr-app/mods/
./install.sh --force    # replace an existing copy
```

`HERDR_APP_HOME` selects another app home.

**Watch out:** the app docks the strip into whichever layout is shown at install
time (the first time it sees the mod folder). Other layouts don't get it, so switch
to the layout you want before running `install.sh`.

### Credentials

The helper reads `windsurf_api_key` and the optional `api_server_url` from the TOML
file named by `DEVIN_CREDENTIALS`. If unset, it reads
`~/.local/share/devin/credentials.toml`. The default API server is
`https://server.codeium.com`.

## Uninstall

```sh
rm -rf ~/.config/herdr-app/mods/devin-limits
```

The app then drops the strip. The last values are kept in the app's `shared.json` under
the `devin-limits` key; they're harmless, and the app owns that file.

## Privacy

The helper reads the Devin credentials TOML locally and sends the API key to the
configured Devin API server to fetch account quota status. It does not print or save
the key.

The endpoint (`exa.seat_management_pb.SeatManagementService/GetUserStatus`) is the
internal, undocumented one Devin CLI's `/usage` panel reads. A CLI or server update may
rename its fields; the strip then shows `unavailable` (or the last values, stale).

## Development

```sh
python3 -m unittest discover -s tests -v   # helper tests against a local fake server
herdr-app check mods/devin-limits
```

## License

MIT
