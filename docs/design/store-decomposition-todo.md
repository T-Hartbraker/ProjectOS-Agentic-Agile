# store.py decomposition (future)

`store.py` currently mixes portfolio, governance, Slack, and delivery-adjacent persistence.

## Proposed bounded repositories

| Module | Responsibility |
|--------|----------------|
| `store/slack_bindings.py` | `slack_bindings` CRUD |
| `store/slack_message_refs.py` | `slack_message_refs` CRUD |
| `store/governance.py` | governance decisions and audit |
| `store/portfolio.py` | project registry mirrors and portfolio views |

## Migration approach

1. Move one table family at a time with re-export shims in `store.py`.
2. Keep SQL and row-shape contracts identical during transition.
3. Add focused tests per repository before deleting shim exports.

## Non-goals for consolidation pass

- No schema changes
- No caller renames outside store internals
