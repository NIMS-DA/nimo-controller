# nimo-controller

## Installation

Using [uv](https://docs.astral.sh/uv/) is recommended for development:

```
git clone https://github.com/NIMS-DA/nimo-controller.git
cd nimo-controller
uv sync
```

## Examples

### Bayesian optimization

The `example_mcp/optimization` directory contains an example MCP server that implements two-dimensional [test functions for optimization](https://en.wikipedia.org/wiki/Test_functions_for_optimization).

Start the MCP server:

```
cd example_mcp/optimization
uv run mcp_test_function.py
```

In a separate terminal, launch NIMO Controller:

```
cd nimo-controller
uv run nimo-controller
```

Then open `http://127.0.0.1:8888/` in your browser.
The workflow workspace is initially empty. The agent text box appears when `enable_agent` is set to `true` in `config.yaml`.

![screenshot1](./screenshots/screenshot1.png)

Click **Manage MCP servers**, enter a name for the server, and use `http://127.0.0.1:8001/mcp` as its URL.

![screenshot2](./screenshots/screenshot2.png)

Click the `Upload candidates file` button and upload `example_mcp/optimization/candidates.csv`.
Once uploaded, the `x1` and `x2` blocks will appear in the NIMO toolbox.

![screenshot3](./screenshots/screenshot3.png)

Use the blocks to build the workflow shown below:

![screenshot4](./screenshots/screenshot4.png)

Press the `Run` button to execute the workflow.

![screenshot5](./screenshots/screenshot5.png)

## Configuration

Configuration is loaded from the following sources, in order of precedence:

1. `./config.yaml` in the current directory
2. `<user-config-dir>/nimo-controller/config.yaml` (for example, `~/.config/nimo-controller/config.yaml`)
3. Built-in defaults (used when no config file exists)

Set startup options in `config.yaml` before launching the app:

```yaml
port: 8888

mcp_servers:
  sdl: "http://127.0.0.1:8001/mcp"

enable_agent: true

# Optional: where candidates.csv and results/ are stored.
# Defaults to ~/.nimo-controller if omitted.
# data_dir: "~/my-nimo-data"
```

## Using Agents

To enable the agent, set `enable_agent: true` in `config.yaml` before starting the app.

Choose **OpenAI**, **Anthropic**, or **Ollama** in the settings or `config.yaml`.
You need to sign in with ChatGPT, set the appropriate API-key environment variable before launch, or enter your Ollama server's base URL to use LLMs.
See the comments in `config.yaml` for details.
