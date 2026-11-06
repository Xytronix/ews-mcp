# Contributing

## Layout

```
src/            the server (python -m src.main)
tests/          pytest suite
docs/           tool reference, troubleshooting, impersonation
v5/             a separate server with its own README and CI
Dockerfile      container build
.env.example    every setting with comments
```

## Tests

The suite needs the system packages from the image, so run it there:

```bash
docker build -t ews-mcp .
docker run --rm -v "$PWD:/app" -w /app --entrypoint sh ews-mcp \
  -c "pip install -q pytest pytest-asyncio && python -m pytest tests -q"
```

## Pull requests

- Keep changes small and in the surrounding style.
- Add a test for behaviour a user would notice, and make sure the whole suite
  passes.
- Use example.com addresses and placeholder credentials. Never commit real
  mailboxes, hostnames or tokens.
- Update the README or `docs/` when a setting or tool changes.

## Security

Report vulnerabilities privately through GitHub's security advisory form, not
in a public issue.

## License

Contributions are licensed under the project's MIT license.
