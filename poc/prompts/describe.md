You document one table of a database that has no documentation. For each column you
get what is known: its name (unless names were withheld), value statistics, sample
values, the other columns of the table, sample rows, and the joins discovered for it.
When a column points at a small lookup table, a few of that table's code -> label
pairs are included.

For the table and for every column, infer what it means from this evidence.
Do not guess: if the evidence does not support a meaning, say so and mark it unresolved.

Reply with JSON only:
{
  "table": {"name": "<short readable table name>", "purpose": "<one sentence>"},
  "columns": {
    "<column>": {
      "role": "identifier" | "date" | "numeric" | "category" | "free_text" | "boolean",
      "name": "<short readable column name>",
      "description": "<one sentence>",
      "status": "resolved" | "tentative" | "unresolved"
    }
  }
}

resolved: the evidence clearly supports the meaning.
tentative: plausible, but another reading fits too.
unresolved: not enough evidence; leave name and description empty.
Include every column you were given, using its exact key.
