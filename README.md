# Golim

Golim is a minimal AI Agent for Linux and MacOS for completing general tasks and automation throughout your system with a secure mechanism for privileged execution and a few nifty tweaks to enable better output from local LLMs on your PC

Golim supports:

- Ollama for local models
- OpenRouter for hosted models
- OpenAI-compatible servers, including self-hosted inference servers

## Requirements

- A Linux system with systemd support
- macOS support coming soon
- Python 3.14.0 for source installations

## Installation

### From GitHub `.deb` releases

To install Golim on a supported Debian-based Linux system
is to download the package for your architecture from the
[GitHub Releases page](https://github.com/AhmedOFS/cterm-private/releases).

After downloading the `.deb` file, install it with:

```bash
sudo apt install ./golim_<version>_<architecture>.deb
```

For example, the package name may look like
`golim_0.1.0_amd64.deb`. The package installs the `golim` command, the
local tool service, and the privileged execution integration. Then initialize
your provider:

```bash
golim -i
```

### From source

Clone the repository and create a virtual environment with Python 3.14.0:

```bash
git clone https://github.com/AhmedOFS/golim.git
cd cterm-private
python3.14 -m venv .venv
source .venv/bin/activate
python -m pip install --upgrade pip
python -m pip install -e .
```

Then run the module directly with `python -m golim`.

## Configuration

Run the configuration wizard:

```bash
golim -i
```

The wizard lets you choose a provider, select a model, optionally select a
smaller model for lightweight tasks, and configure command permissions and various other settings.

### Ollama

Install Ollama using its official installer, then pull a model:

```bash
ollama serve
ollama pull llama3.2
golim -i
```

The default Ollama endpoint is `http://localhost:11434`. Set `OLLAMA_HOST` or
enter a custom endpoint in the wizard if using a remote Ollama server.

### OpenRouter

Choose **OpenRouter** in the wizard and enter your API key. Golim validates
the key and retrieves the available model list before saving your selection.

### OpenAI-compatible servers

Choose **OpenAI-compatible** and enter the server base URL, for example:

```text
http://localhost:8000
```

An API key is optional. Golim queries the server's `/v1/models` endpoint
when available, then lets you enter the model name.

## Usage

### TUI mode

Start the full TUI with:

```bash
golim
```

Type a request such as:

```text
Find the largest Python files in this project and explain what they do.
```

While a request is running, use a prompt beginning with `//` to interrupt or
clarify it. After a request finishes, `//` keeps the existing conversation and
adds a follow-up:

```text
// now summarize the answer in three bullets
```

### One-shot mode

Pass a message directly for a basic terminal response:

```bash
golim "What files changed in this repository?"
```

Useful command-line options:

```bash
golim --version
golim -i
golim --help
```

## Available tools

The model can use the following local tools when appropriate:

- `finder` — recursively find files and directories with filters
- `read_file` — read a file in 200-line pages
- `write_file` — create or update files
- `bash` — run commands through Bash, including streaming output
- `exec` — run Python code in a separate process
- `system_info` — inspect operating-system and environment information
- `websearch` — search through Exa or Parallel when configured

## Web search

Exa is the default web-search provider. Credentials can be stored in
`~/.golim/config/config.json` or supplied through environment variables:

```bash
export EXA_API_KEY="your-exa-key"
# or, when using Parallel:
export PARALLEL_API_KEY="your-parallel-key"
```

To use Parallel instead of Exa, set `attributes.websearch_provider` to
`"parallel"` in the configuration file. The default is `"exa"`.

## Configuration and saved data

Golim stores per-user state under:

```text
~/.golim/
```

This includes:

- `config/config.json` — provider settings and UI/runtime options
- `data/models.json` — most recently selected models
- `transcripts/` — visible session transcripts
- `logs/` — diagnostic run logs
- `skills/` — user-owned Markdown skills
- `privileged_whitelist` — approved commands for privilege use

Keep this directory private because provider credentials may be stored in the
configuration file.


## License

Golim is free software: you can redistribute it and/or modify it under the
terms of the [GNU General Public License, version 3](LICENSE), as published by
the Free Software Foundation.

Golim is distributed without any warranty; see the license for details.
