# Frontend Instructions

The operator dashboard is Vue 3 with Pinia, Vue Router and Vite. Source lives under `src/`; `dist/` is generated and served by FastAPI after a build.

- `src/services/api.js` owns HTTP calls and `src/services/events.js` handles server-sent run events. Preserve auth/session behavior and keep transport details out of components.
- `src/stores/` owns shared run/check state; components and views should render state and dispatch store actions rather than duplicating workflow logic.
- Keep component names `PascalCase.vue`, store/util APIs lower camel-case, and operator-facing copy in Russian.
- When displaying prepress results, distinguish errors, warnings and pending operator confirmation. Show the 270 DPI threshold and ICC/color details consistently with the backend DTO.
- Keep accessibility and existing interaction behavior for previews, order filters, confirmation and return reasons.
- Add UI state tests beside source as `*.test.js`; run `npm test -- --run` and `npm run build` when frontend behavior changes. Do not hand-edit `dist/`.
