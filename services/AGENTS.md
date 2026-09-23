# Services Instructions

`services/` coordinates domain workflows without owning HTTP transport details. Keep services injectable and independently testable.

- `coordinator.py` owns the single background run worker, queueing, progress/events, cancellation and confirmation pauses. Preserve valid run/order state transitions and repository event ordering.
- `domain.py` defines statuses and allowed operator transitions. Do not bypass transition validation in action handlers.
- `batch_adapter.py` is the boundary between processing and run coordination; map options/results here rather than coupling the coordinator to `BatchProcessor` internals.
- `order_workflow.py` handles operator decisions and background external actions. Preserve idempotency/locking so duplicate requests do not send duplicate actions.
- `file_lifecycle.py` moves/copies order files and creates previews. Preserve originals and use explicit conflict strategies; generated artifacts stay in designated folders.
- `repository.py` and `sql_repository.py` implement run persistence. Update DTO/repository serialization consistently when run fields change.
- `pitstop/`, `sborka_integration.py` and `ftp_preview_uploader.py` are external integration boundaries. Inject transports; tests must use fakes and must not contact production.
- Service-boundary APIs should use type hints. Keep operator-facing Russian messages actionable. Add focused tests under `tests/`.
