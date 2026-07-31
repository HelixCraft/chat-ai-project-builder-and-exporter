# Chat AI Project Builder and Exporter

> **Turn chat-AI output into real project files on your disk.**

## What it is

`chat-ai-project-builder-and-exporter` is a small command-line tool that takes a text output produced by a browser-based chat AI (ChatGPT, Claude, Gemini, etc.) and turns it into a real, ready-to-use project folder on your computer. Browser-based AIs cannot create files for you — they can only output text. This tool bridges that gap: you save the chat output as a text file (or copy it to the clipboard), run `chat-ai-project-builder-and-exporter`, and seconds later you have the complete project structure with every file in the right place, ready to open in your editor or run immediately.

## How it works

The AI outputs the project in a simple, human-readable text format called **UPEP** (Universal Project Export Protocol). Each file is announced with a `### FILE:` line followed by its content and an `### END OF FILE` marker. The tool reads this text — from a file or directly from the clipboard — validates every path for safety, creates any missing folders, and writes each file to disk with its exact original content (including Unicode, line endings, and empty files). Optionally, it can also bundle the result into a ZIP archive. The whole process is single-pass, safe against path-traversal attacks, and uses only Python's standard library (no dependencies to install).

## Installation

No installation required. You only need **Python 3.9 or newer**.

1. Download or clone this repository.
2. Open a terminal in the project folder.
3. Run it directly:

```bash
python3 main.py --help
```

That's it — no `pip install`, no virtual environment, no dependencies.

> **Linux users:** If you want to use the `--clipboard` (`-c`) flag, you need one extra system tool depending on your session:
> - X11: `sudo apt install xclip`
> - Wayland: `sudo apt install wl-clipboard`
>
> Windows and macOS need nothing extra — clipboard support works out of the box.

## Usage

1. Give the [UPEP prompt](https://github.com/your-username/UPEP-prompt) to your chat AI so it knows to output the project using the protocol.
2. Copy the AI's output and feed it into the tool:

```bash
python3 main.py [input.txt | -c] [output_directory] [-a]
```

You must provide **exactly one** input source: either a file path or the `-c` (clipboard) flag. Everything else is optional.

### Flags

| Flag             | Description                                                                       |
|------------------|-----------------------------------------------------------------------------------|
| `input.txt`      | Path to the UPEP text file produced by the chat AI. Optional if `-c` is used.    |
| `output_directory` | Where to create the project. Defaults to the current directory.                |
| `-c`, `--clipboard` | Read the UPEP input from the system clipboard instead of a file. Works on Windows, macOS, Linux/X11 and Linux/Wayland. |
| `-a`, `--archive`   | Also create a ZIP archive of the reconstructed project.                     |
| `-h`, `--help`      | Show the help message and exit.                                             |

### Example — What the input looks like

**A minimal UPEP file that the tool understands:**

````text
### PROJECT TREE

```text
myproject/
├── main.py
└── README.md
```

### FILE: main.py

print("Hello, world!")
### END OF FILE

### FILE: README.md

# My Project

A simple example.
### END OF FILE
````

When you run the tool on this file (or paste it to the clipboard and use `-c`), you get:

```
myproject/
├── main.py        ← contains: print("Hello, world!")
└── README.md      ← contains: # My Project\n\nA simple example.
```

## License

MIT — free to use, modify, and share.
