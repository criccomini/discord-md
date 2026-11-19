# discord-md

discord-md turns selected Discord channels, public threads, and forum posts into searchable Markdown for Perch. It runs continuously and keeps transcripts up to date as people post, edit, and delete messages.

## 1. Create a Discord bot

1. Create a bot in the [Discord Developer Portal](https://discord.com/developers/applications) and enable **Message Content Intent** on the **Bot** page.
2. Install the bot in each server with the `bot` scope. Give it **View Channel** and **Read Message History** in the channels you want to export.
3. In Discord, turn on **Developer Mode** under **User Settings → Advanced**. Right-click each server and channel to copy its ID.

## 2. Install and configure

Run these commands from the checkout on the Linux machine where Perch runs. Use Python 3.11 or later.

```sh
python3 -m venv ~/.local/share/discord-md/venv
~/.local/share/discord-md/venv/bin/pip install .

config_dir="${XDG_CONFIG_HOME:-$HOME/.config}/discord-md"
install -d -m 700 "$config_dir"
install -m 600 config.example.toml "$config_dir/config.toml"
(umask 077 && touch "$config_dir/token.env")
```

Replace the sample IDs in `config.toml`. Add a `[[channels]]` entry for each channel, with a unique lowercase path such as `slatedb/dev`. In `token.env`, add `DISCORD_TOKEN=your-bot-token`.

## 3. Start the service

```sh
install -Dm644 systemd/discord-md.service "${XDG_CONFIG_HOME:-$HOME/.config}/systemd/user/discord-md.service"
systemctl --user daemon-reload
systemctl --user enable --now discord-md.service
```

View logs with `journalctl --user -u discord-md.service -f`. To start the service at boot and keep it running after logout, run `loginctl enable-linger "$USER"`.

## 4. Add the export to Perch

For channels with paths like `slatedb/dev`, add `discord/slatedb` in **Settings → Knowledge** and select the repositories that should use it. Start a new Perch agent session to pick it up.

The bot writes to `~/.perch/knowledge/discord` by default. Set `output_dir` if Perch uses a different knowledge folder. Each project gets an `INDEX.md` with links to transcripts, and each message links back to Discord.

## History and recovery

The bot imports 30 days of history by default (`backfill_days`). It saves its place in SQLite and catches up on new messages after downtime. When it starts a new Discord session, it also rechecks seven days of history for missed edits and deletions (`reconcile_days`). Set both options in `config.toml`.
