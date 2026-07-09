# udev integration

Install ReSpeaker Flex XVF3800 USB access rules so the desktop-assistant
service can talk to the XVF host-control endpoint:

```bash
sudo install -D -m 0644 services/udev/70-respeaker-flex-xvf.rules /etc/udev/rules.d/70-respeaker-flex-xvf.rules
sudo udevadm control --reload-rules
sudo udevadm trigger --attr-match=idVendor=2886 --attr-match=idProduct=0022
```

The rule assigns the raw USB control node to group `plugdev` with mode `0660`.
Make sure the service user belongs to `plugdev`.
