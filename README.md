# nimo-controller

## Installation

Installation requires [uv](https://docs.astral.sh/uv/).

```
git clone https://github.com/NIMS-DA/nimo-controller.git
cd nimo-controller
uv sync
```

## Examples

### Bayesian optimization

The `example_mcp/ackley` directory contains an example MCP server that demonstrates how to use PHYSBO with the [Ackley function](https://en.wikipedia.org/wiki/Ackley_function).

Start the MCP server:

```
cd example_mcp/ackley
uv run mcp_ackley.py
```

In a separate terminal, launch NIMO Controller:

```
cd nimo-controller
uv run nimo-controller
```

Then open `http://127.0.0.1:8888/` in your browser.
At this point you will see a warning indicating that no candidates file has been loaded yet.

![screenshot1](./fig/screenshot1.png)

Click the `Upload candidates file` button and upload `example_mcp/ackley/candidates.csv`.
Once uploaded, the `x1` and `x2` blocks will appear in the NIMO toolbox.

![screenshot2](./fig/screenshot2.png)

Use the blocks to build the workflow shown below:

![screenshot3](./fig/screenshot3.png)

Press the `Run` button to execute the workflow.

![screenshot4](./fig/screenshot4.png)

Note that the target is the *negated* Ackley function, since PHYSBO performs maximization.

### Phase diagram construction

The `example_mcp/phase_diagram` directory contains an example MCP server that demonstrates the PDC algorithm by constructing a phase diagram for the fictional material X described in the [IvoryOS integration guide](https://nims-da.github.io/nimo/en/ivoryos.html).

Start the MCP server:

```
cd example_mcp/phase_diagram
uv run mcp_phase_diagram.py
```

Restart NIMO Controller, then upload `example_mcp/phase_diagram/candidates.csv` via the `Upload candidates file` button.

Build the workflow shown below. After running the workflow, a phase diagram will be constructed.

![screenshot5](./fig/screenshot5.png)

## Configuration

Configuration is loaded from the following sources, in order of precedence:

1. The path specified by `--config <path>`
2. `./config.yaml` in the current directory
3. `<user-config-dir>/nimo-controller/config.yaml` (for example, `~/.config/nimo-controller/config.yaml`)
4. The default configuration bundled with the package

Example `config.yaml`:

```yaml
model: "gpt-4o-mini"
port: 8888

# Optional: route requests to an OpenAI-compatible local endpoint (e.g. Ollama).
# openai_base_url: "http://127.0.0.1:11434/v1"
# openai_api_key: "ollama"

mcp_servers:
  sdl: "http://127.0.0.1:8001/mcp"
```

Runtime data, including the uploaded `candidates.csv` and optimization results, is written to `<user-data-dir>/nimo-controller/` (for example, `~/.local/share/nimo-controller/`).
