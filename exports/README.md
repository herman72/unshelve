# Exports of your Telegram chats

Put the folder of an export here, for example `exports/ChatExport_2026-10-13`. Then import it:

```bash
docker compose exec api unshelve import /imports/ChatExport_2026-10-13 --timezone Europe/Helsinki --dry-run
```

Git ignores everything in this folder, except this file. So an export stays on your computer.

The stack shows this folder to the `api` container as `/imports`, and the container can only read it. [The importer note](../docs/learn/11-export-importer.md) explains how to make an export and what the command does.
