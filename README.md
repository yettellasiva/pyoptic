# Optical transfer (QR stream)

Send a file from laptop A to laptop B with no network or USB. A shows the file as a looping
stream of QR codes on its screen, and B reads them with its webcam.

- `sender.py`: runs on laptop A
- `receiver.py`: runs on laptop B
- `protocol.py`: packet format, used by both

## Install (offline laptops)

On any **online** Windows machine, download the wheels for the Python version on the offline
laptops (here 3.12; if a laptop has 3.13, run the command again with `--python-version 3.13`):

```powershell
py -m pip download -r requirements.txt -d wheels --only-binary=:all: --platform win_amd64 --python-version 3.12
```

Copy this folder, including `wheels/`, to both laptops (any way you can), then on each one run:

```powershell
py -m pip install --no-index --find-links wheels -r requirements.txt
```

The sender only needs `segno`, but installing everything on both laptops is simpler.

## Use

1. **Laptop B:** run `py receiver.py`, click **Start camera** and aim the webcam at laptop A's screen.
   The preview draws a green box around every QR it reads.
2. **Laptop A:** run `py sender.py`, pick the file and press **Start** (Space). Press **F11** for fullscreen.
3. The sender loops forever, so frames B misses come around again on the next pass.
   To skip a full pass at the end, copy the receiver's **Missing chunks** list into the sender's
   **Resend only chunks** field, then pause and restart the sender (Space twice).
4. When every chunk has arrived, the receiver computes the SHA-256 of the rebuilt file and compares
   it with the hash the sender announced. It shows **✓ VERIFIED** and saves the file only if they match.

## How the hash check works

- The sender computes the SHA-256 of the file and shows it in its panel (and in the bottom-left
  corner in fullscreen). It also sends it inside the metadata QR frame.
- The receiver shows two hashes: the one announced by the sender, and the one it computed
  from the received bytes. If they differ, the file is not saved.
- Each frame also carries a CRC32, so corrupted reads are dropped silently.
- For a check that doesn't depend on the transfer at all, compare the large short hash
  (`AB12CD34 … 9F8E7D6C`) on both screens by eye.

## Settings

| Setting | Default | Notes |
|---|---|---|
| Bytes per QR | 800 | Bigger means fewer frames but denser codes that need a sharp camera. 500–1500 is typical. |
| Error correction | M | L lets you send more bytes per QR; Q/H cope better with blur and glare. |
| Frames / second | 8 | Keep it well below the camera fps (usually 30). |
| Camera index / resolution | 0 / 1280x720 | Try 1920x1080 for dense codes if the webcam supports it. |

Rough speed: at 800 B × 8 fps, 10 MB takes about 13,000 frames ≈ 28 min per pass. At 1500 B × 12 fps it's ≈ 10 min.
Tune on a small test file first.

**Tips:** set screen brightness to maximum, turn off night light, avoid reflections, and fill most of
the webcam image with the QR. Keep the camera 20–40 cm from the screen.
