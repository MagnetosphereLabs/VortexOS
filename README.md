# VortexOS
A new kind of computing experience that puts *you* in control.

This is a work in progress, with full documentation on how to use this software being added here once it is ready for release.

It is not likely to cause issues and it works great on our Ubuntu servers, but since this is unfinished software we give a warning about installing the alpha build only on a Debian based system that you have nothing important on. If you wish to install Vortex, simply run:

```
curl -fsSL https://raw.githubusercontent.com/MagnetosphereLabs/VortexOS/main/vortex_installer.sh | sudo bash
```

To view live logs, simply use this command:
```
sudo journalctl -u vortex-node.service --lines=1000 --follow
```

To uninstall our alpha build of VortexOS, simply run this command:
```
curl -fsSL https://raw.githubusercontent.com/MagnetosphereLabs/VortexOS/main/vortex_uninstall.sh | sudo bash -s -- --yes
```
