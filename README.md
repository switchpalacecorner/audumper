# audumper

Dump data from AU phones using a bug in the Picsel Document Viewer and a standard AU data USB cable.
This tool pushes two documents over the standard USB data link, then listens for the dump data.
Opening the two documents in order triggers the dump.

Currently supported:

- ca003:
  - dump_fs: sweeps the EMMC filesystem and rebuilds it as a directory tree
  - dump_nand: dumps nand and oob
- w47t:
  - dump_nand: dumps nand and oob
- w53ca:
  - dump_nand: dumps nand and oob from both nand chips
- sh002:
  - dump_fs: sweeps the EMMC filesystem and rebuilds it as a directory tree
  - dump_nand: dumps nand and oob

## Install

Linux only, and sudo is needed for raw USB access. Set up a virtual
environment in the tool directory:

```
sudo python3 -m venv .venv
sudo .venv/bin/pip install -r requirements.txt
```

## Usage

```
sudo .venv/bin/python audumper.py --list
sudo .venv/bin/python audumper.py --p MODEL COMMAND
```

A model and a command are both required. --list shows what is installed, and

```
sudo .venv/bin/python audumper.py --p MODEL --help
```

prints everything about one phone: location of USB settings, USB mode to select, where
the pushed files appear, and what each of its commands does.

## Prep on the phone

Before starting a dump:

- Most AU data cables do not provide power. Ideally the phone should be run off a
  power supply while connected, since the dumps can take a while. If you don't have one,
  make sure the battery is fully charged
- USB mode set to *data transfer*, not mass storage. using --help and --p to specify a model will tell you the
  specific location and name of the setting for the model.
- sitting at standby, with no transfer screen already open. Having a menu open usually blocks transfer

The tool pushes both files and then begins listening for the dump data to come back.
The tool will tell you where to find them, since not every model puts them in the same place.
Whichever folder they're in, they should always appear at the top.
If you have trouble finding them (or finding the usb settings), most of these phones have an English setting which should make it easier.
The two files are named after the command, so dump_nand pushes NAND01 and NAND02.
Open the 01 file first (the document should just open and not do anything, this is correct).
Close that document, then open the 02 file. The dump should then begin on the terminal,
do not touch the phone until it finishes.
Don't touch *anything* after pressing ok to open the second file. You may get a render error, that's
expected and you should just leave the error popup open while the dump proceeds. The phone will probably
fall asleep and blank the screen during the dump, this is also fine
At the end, the phone should be powered down.

Always power cycle the phone before running another payload. The documents will be left behind
and can be deleted through the phone UI.

## Re-running without pushing

The documents stay on the phone after a run, so a repeat only needs the listener:

```
sudo .venv/bin/python audumper.py --p MODEL COMMAND --no-push
```

That skips the USB push, the handshake and the init entirely, prints the same
instructions and goes straight to listening. Use it after a power cycle, if
a run was interrupted, or if the push worked but the link dropped before the
capture. Still power cycle the phone first.

## Other options

```
-v, --verbose   log more USB details. Useful for debugging if your dump is failing
--out DIR       where to create the run folder. Not required, default is in this directory alongside the script.
--framesize N   override the model manifest's frame size.
```
