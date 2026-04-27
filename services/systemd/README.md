# systemd integration

To install the service unit:

```bash
sudo cp services/systemd/desktop-assistant.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now desktop-assistant.service
```

To follow logs:

```bash
journalctl -fu desktop-assistant
```

To stop / disable:

```bash
sudo systemctl stop desktop-assistant
sudo systemctl disable desktop-assistant
```

The unit file assumes the project lives at
`/home/starter/Code/Desktop Assistant` and runs as user `starter`.
Edit the unit if your layout differs.
