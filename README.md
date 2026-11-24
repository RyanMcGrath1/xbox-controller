# Xbox Remote

A controller you open in a browser on your laptop or phone. It talks to the Xbox over Wi-Fi with [xbox-smartglass-core](https://pypi.org/project/xbox-smartglass-core/).

You can wake and shut down the console, see what is running, move around the dashboard, control media, type into a text field, and ask for a Game DVR clip.

## Run

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
pip install xbox-smartglass-core==1.3.0 --no-deps
python app.py
```

The second install skips the library's 2020 dependency pins. `cryptography==3.2.1` has no Apple Silicon wheel and does not build on current macOS. The versions in `requirements.txt` are the ones this remote is meant to run with.

Open the laptop link it prints. On your phone, join the same Wi-Fi and open the phone link.

## On the Xbox, once

1. Settings → General → Power mode → **Instant-on**, so Power can wake it.
2. Settings → Devices & connections → Remote features → turn remote features on and allow connections from **any device**.

Find the console while it is on. After that, Power still works when it is asleep.

Dashboard and app buttons go through SmartGlass. Gameplay still needs a real controller. Game DVR can refuse the request unless the console accepts this remote as an allowed device.
