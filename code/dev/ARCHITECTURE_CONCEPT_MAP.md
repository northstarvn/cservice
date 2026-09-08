# CService Concept Map

## Revision Control

- Revision ID: `r4`
- Scope: repository-wide conceptual model for the current workspace state
- Purpose: provide a stable, top-down structure that can be revised repeatedly without changing the document's shape
- Reading rule: each layer reveals only the next level of detail; missing pieces are listed explicitly as gaps instead of being inferred
- Stop rule: once the revision closure rule is satisfied, treat the remaining backlog as a future revision rather than extending the current one
- Resume rule: the next revision should reopen only the narrowest implementation slice needed for the selected backend owner

## 1. Executive View

This repository is a backend-centered customer-service platform built around FastAPI, async SQLAlchemy, and a set of domain routers for users, bookings, chat, and system metadata. The backend also exposes deeper analytics, retention, lifecycle, and ecosystem metadata that define the product's operational intelligence layer.

All backend control surfaces should be derived from either system-only metrics, including AI-generated metrics, or customer-centric point-ranking metrics. That includes functionality access, topic interest, user-to-user proximity, and community proximity, with the scoring kept simple enough for improvement and feedback workflows to monitor directly.

The implementation is best understood as three overlapping backend systems:

1. Identity, account, and auth control plane
2. Booking, chat, and interaction state plane
3. Analytics, retention, and operational insight plane

The current workspace contains both implemented backend surfaces and requirement artifacts that describe a broader intended product. The document therefore distinguishes between:

- Implemented behavior
- Intended behavior from requirement files
- Gaps, mismatches, or duplicated concepts

## 2. Top-Level System Shape

### 2.1 Frontend

The frontend exists in the repository, but it is outside the scope of this revision. This concept map focuses on backend implementation and backend-adjacent requirement artifacts only.

### 2.2 Backend

The backend is a FastAPI application with async SQLAlchemy, token-based auth, domain routers, service helpers, and metadata endpoints. It is organized around user identity, booking lifecycle management, chat analytics, retention intelligence, and ecosystem readiness reporting.

Primary entry chain:

- `fastapi/app/main.py`
- `fastapi/app/db.py`
- `fastapi/app/models.py`
- `fastapi/app/routers/users.py`
- `fastapi/app/routers/bookings.py`
- `fastapi/app/routers/chat.py`

Primary service layer:

- `fastapi/app/services/bookings.py`
- `fastapi/app/services/chat_analytics.py`
- `fastapi/app/services/retention.py`

Primary schema layer:

- `fastapi/app/schemas/schemas.py`
- `fastapi/app/schemas/chat.py`
- `fastapi/app/deps.py`

### 2.3 Requirement Layer

The `requirement/` folder acts like a product brief and intended architecture archive. It describes menus, screens, multilingual behavior, testing expectations, and a service-planning flow that does not fully match the current app routing.

Key artifacts:

- `requirement/core_structure.json`
- `requirement/services_and_planning.json`
- `requirement/multilingual_and_testing.json`
- `requirement/security_spec.js`

## 4. Backend Concept Model

### 4.1 Startup and Environment

`fastapi/app/main.py` constructs the app, configures CORS, verifies the database on startup, and registers exception handling. It exposes metadata endpoints that describe the app, capabilities, ecosystem status, and authenticated probe routes.

At a conceptual level, the backend startup owns:

- App identity and version
- Database readiness
- Cross-origin access policy
- Global error shaping
- Router registration

### 4.2 Data and Domain Substrate

`fastapi/app/models.py` defines the persisted data model that everything else builds on. The important clusters are:

- Identity and access: `User`
- Conversation history: `ChatHistory`
- Booking lifecycle: `Booking`, `BookingEvent`, `BookingStatus`, `ServiceType`
- Behavioral signals: `InteractionSignal`
- Retention history: `RetentionSnapshot`

These objects show that the backend is not only transactional. It also preserves state transitions and interaction traces so the system can explain customer behavior over time.

### 4.3 Identity and Auth API

`fastapi/app/routers/users.py` supports registration, login, current-user lookup, and password change. This is the account control plane for the app.

`fastapi/app/deps.py` resolves the authenticated user from the bearer token and enforces the admin boundary when needed.

Important behaviors:

- JWT decoding via the shared security secret
- Database-backed user lookup by username
- Admin-only enforcement through `get_current_admin_user`

### 4.4 Booking Lifecycle API

`fastapi/app/routers/bookings.py` supports booking creation, listing, reading, editing, deleting, audit/history summaries, and an assignment endpoint that can persist caller-provided placement details, update the latest persisted assignment, and keep a single current assignment authoritative. The router is backed by `fastapi/app/services/bookings.py`, which contains reusable lifecycle rules plus a deterministic assignment helper.

Important service behavior:

- Booking updates are normalized before persistence
- Status transitions are validated before mutation
- Transition helpers record booking events alongside state changes
- Ownership checks are centralized in the service layer
- The booking service now also builds, persists, updates, and current-flags an explainable assignment report from the current booking and user context
- Assignment changes are emitted as booking events so the assignment slice participates in the same history trail as other booking mutations

This router is still primarily a lifecycle model, but it now carries the first concrete placement-style decision surface.

### 4.5 Chat Analytics and Recovery API

`fastapi/app/routers/chat.py` is the broadest surface. It includes chat history, sentiment analysis, interaction insights, dissatisfaction recovery, loyalty recovery, retention dashboards, maintenance reports, risk profiles, system priorities, monetization cohorts, operational readiness views, and the consolidated retention snapshot operations report. The heavy lifting is concentrated in `fastapi/app/services/chat_analytics.py` and `fastapi/app/services/retention.py`.

Important service behavior:

- Sentiment scoring drives recovery classification
- Keyword, recency, booking state, and history analysis produce policy-area insights
- Recovery reports turn dissatisfaction signals into explicit action plans and retention recommendations
- Recovery outputs now carry evidence summaries, source labels, and stronger policy-area recommendations
- Retention and cohort reports distinguish loyalty, monetization readiness, and signal pressure as separate dimensions
- Retention snapshots summarize loyalty, churn risk, and lifecycle stage
- Retention reports can derive deltas and trends from persisted snapshots
- Retention snapshot operations reports make freshness, cleanup, and readiness explicit
- Topic ranking now has a paired policy layer through `TopicPolicyDecision` and `TopicPolicyDecisionReport`
- Topic policy decisions classify ranked areas into allowed, blocked, or review-required outcomes with rule-versioned rationale

The chat router is therefore both a conversation API and a business-intelligence API.

### 4.6 Metadata and Ecosystem Layer

`fastapi/app/main.py` exposes capability and ecosystem endpoints that describe the backend as a system of cooperating subservices.

This layer is important because it turns the backend into an observable platform rather than a hidden API.

Key surfaces:

- `/meta` for app identity
- `/meta/capabilities` for capability summaries
- `/meta/ecosystem` for subservice status
- `/meta/probe/routes` for authenticated route probing
- `/chat/admin/snapshot-operations-report` for consolidated snapshot operations reporting

### 4.7 Schema Layer

`fastapi/app/schemas/schemas.py` and `fastapi/app/schemas/chat.py` define the shape of all request and response payloads.

This layer is the contract boundary between routers, services, and clients. It formalizes:

- Booking lifecycle payloads
- User and token payloads
- Analytics, recovery, and retention reports
- Ecosystem and capability reports
- Topic ranking and topic policy decision reports

## 5. Requirement-Layer Intent

The requirement files describe a more formal customer-service product language than the backend currently exposes directly.

### 5.1 Intended Service Scope

`requirement/core_structure.json` and `requirement/services_and_planning.json` describe a broader customer-service system centered on booking, chat assistance, planning, and language-aware behavior. These artifacts are useful as product intent, but they are not the primary implementation surface in this revision.

### 5.2 Intended Multilingual Model

The multilingual spec expects language files and selector-driven switching among at least English, Spanish, and French. Backend support for language-aware behavior exists only indirectly through stored user preferences and request handling patterns; a centralized localization service is not yet evident in the backend.

## 6. Layered Walkthrough From User Intent to Data

### Layer 1: Incoming request

A client or internal tool sends requests for auth, bookings, chat, or metadata.

### Layer 2: Dependency resolution

FastAPI dependencies resolve the current user and database session.

### Layer 3: Domain mutation or read

Routers validate the request, apply business rules, and load or mutate the relevant model.

### Layer 4: Persistence

SQLAlchemy persists the change or reads back the requested state.

### Layer 5: Derived analytics

Services and metadata endpoints compute summaries, retention views, cohort reports, or capability descriptions.

### Layer 6: Operational insight

Metadata routes surface ecosystem readiness, health summaries, and authenticated route probes for internal coordination.

## 7. Stable Gap Register

These are the main gaps that should remain visible across revisions.

### 7.1 Requirement-to-backend mismatch

The requirement files describe a broader product surface than the backend currently exposes through implemented routes.

### 7.2 Chat contract stabilization

The chat API now exposes explicit recovery and retention schema contracts for dissatisfaction, loyalty, retention dashboard, and retention operations flows. Clients should still validate against the router, but the contract is no longer only implied by downstream service behavior.

### 7.3 Translation and locale gaps

The requirements imply a stronger localization layer than the backend currently implements. User preference storage exists, but centralized localization services are not yet explicit.

### 7.4 Backend breadth exceeds implemented UI

The backend exposes rich retention and ecosystem endpoints, and the recovery/retention dashboard and retention operations surfaces are now explicit in the schema layer. Some deeper operational views are still primarily visible through metadata rather than through dedicated product flows.

### 7.5 Backlog-to-surface mapping gap

The expanded room allocation, topic modeling, authenticity, legal, observability, privacy, and incident-response backlog still needs a concrete owner map across routers, services, schemas, and metadata endpoints.
The booking assignment slice is now substantially resolved as a persisted, current-flagged, auditable flow, but the broader room-allocation backlog still needs a real placement model before it can be treated as complete.

## 8. Backlog-to-Implementation Map

This section turns the broad backend backlog into the smallest set of implementation surfaces that can own it.

### 8.1 Router ownership

- `fastapi/app/routers/users.py` should continue to own account, consent, identity, and admin-boundary behaviors.
- `fastapi/app/routers/bookings.py` now owns booking lifecycle, assignment reporting, reassignment-adjacent audit flows, and placement-style summaries that can be derived from the current booking context.
- `fastapi/app/routers/chat.py` should own topic selection, recovery classification, retention intelligence, and human-in-the-loop signals that emerge from conversation state.
- `fastapi/app/main.py` should remain the place for ecosystem, capability, readiness, and route-probe surfaces.

### 8.2 Service ownership

- `fastapi/app/services/bookings.py` now owns transition rules, assignment-report construction, and assignment-history rules before any router exposes them.
- `fastapi/app/services/chat_analytics.py` should own topic ranking signals, recovery explanations, retention summaries, and policy-facing analytics outputs.
- `fastapi/app/services/retention.py` should own lifecycle snapshots, longitudinal trend derivation, and operational readiness summaries.
- Any future moderation, legal, or room-allocation logic should be introduced as explicit service helpers rather than folded into router code.

### 8.3 Schema ownership

- `fastapi/app/schemas/schemas.py` should define stable request and response contracts for identity, booking lifecycle, and ecosystem metadata.
- `fastapi/app/schemas/chat.py` should define the structured payloads for recovery, retention, cohort, and operational intelligence outputs.
- New topic, policy, room-assignment, and review payloads should be added as schema-level contracts before they are wired into routers.

### 8.4 Metadata ownership

- `fastapi/app/main.py` should continue to publish health, capability, and ecosystem summaries that explain backend state without requiring internal debugging access.
- Metadata endpoints should be extended before new concepts are surfaced only through private service outputs, so operators can observe behavior early.

### 8.5 Implementation sequencing

- Introduce new matching or policy behavior in services first.
- Add schema contracts second so the new behavior is stable at the boundary.
- Expose it through routers last so the public surface remains predictable.

### 8.6 Minimal next build slice

- Add a deterministic topic-selection helper in `fastapi/app/services/chat_analytics.py`.
- Replace the current booking assignment placeholder with a persisted matching model in `fastapi/app/services/bookings.py` once real room or placement data exists.
- Add schema placeholders for room assignment, policy outcomes, and review decisions in `fastapi/app/schemas/chat.py` or `fastapi/app/schemas/schemas.py`.
- Add metadata reporting for any new room or topic decision surface in `fastapi/app/main.py`.

### 8.7 Backlog trace map

- Room allocation, room state, and reassignment work should start in `fastapi/app/services/bookings.py` and then be reflected in `fastapi/app/routers/bookings.py`.
- Topic ranking, topic provenance, and interest decay work should start in `fastapi/app/services/chat_analytics.py` and then be represented in `fastapi/app/schemas/chat.py`.
- Authenticity, legal filtering, and moderation outcomes should be modeled as service-layer helpers first, then exposed as structured schema fields before any router changes.
- Privacy, consent, and user-control behavior should remain anchored in `fastapi/app/routers/users.py`, `fastapi/app/deps.py`, and the shared schema layer.
- Observability, incident response, and support instrumentation should surface through `fastapi/app/main.py` and the reporting paths already used for capability and ecosystem summaries.
- Governance, rollout safety, and interoperability should be represented as metadata and contract changes before they become new user-facing endpoints.

### 8.8 Priority ladder

1. Stabilize the booking and chat service boundaries so new behavior has a clear owner.
2. Add structured schema contracts for room assignment, topic selection, and policy decisions.
3. Introduce deterministic matching and explanation helpers in the service layer.
4. Surface the new behavior through routers only after the contracts are stable.
5. Extend metadata reporting so operators can observe the new surfaces.
6. Add governance, observability, and support tooling once the core behavior is reliable.

### 8.9 Build phases

- Phase 1: service helpers, schema contracts, and persistence models.
- Phase 2: router exposure, metadata reporting, and audit-visible summaries.
- Phase 3: governance, rollout safety, observability, and human review workflows.
- Phase 4: interoperability, experimentation, continuity, and long-horizon retention behavior.

### 8.10 Dependency matrix

- Booking assignment depends on `fastapi/app/models.py`, `fastapi/app/services/bookings.py`, `fastapi/app/schemas/schemas.py`, and `fastapi/app/routers/bookings.py`.
- Topic intelligence depends on `fastapi/app/services/chat_analytics.py`, `fastapi/app/schemas/chat.py`, and `fastapi/app/routers/chat.py`.
- Authenticity and legal filtering depend on `fastapi/app/deps.py`, `fastapi/app/services/chat_analytics.py`, `fastapi/app/schemas/chat.py`, and the policy-bearing parts of the routers.
- Privacy and consent depend on `fastapi/app/routers/users.py`, `fastapi/app/deps.py`, and shared schema definitions.
- Observability and ecosystem reporting depend on `fastapi/app/main.py` and the reporting payloads already carried through the schema layer.
- Governance and support workflows depend on the service layer, the schema layer, and metadata surfaces before they depend on new public endpoints.

### 8.11 Execution checkpoints

- Confirm the service helper exists before adding new router branches for room allocation or topic ranking.
- Confirm the schema contract exists before exposing any policy, review, or assignment state to clients.
- Confirm the router path uses the shared service helper instead of reimplementing ranking or explanation logic locally.
- Confirm metadata surfaces describe the new capability before it is treated as production-ready.
- Confirm tests cover the new decision path before the backlog item is considered implemented.

### 8.12 Verification anchors

- `fastapi/app/services/chat_analytics.py` should remain the canonical source for topic ranking and interaction insight generation.
- `fastapi/app/routers/chat.py` should consume the shared service output and only handle request and response shaping.
- `fastapi/app/schemas/chat.py` should carry the reusable payloads for insight, recovery, and policy output fields.
- `fastapi/app/main.py` should keep capability and ecosystem reporting aligned with any new backend surface.
- `ARCHITECTURE_CONCEPT_MAP.md` should retain the same layered order so future revisions can add detail without rewriting the document.

### 8.12.1 Topic policy anchor

- `fastapi/app/services/chat_analytics.py` now owns both topic ranking and topic policy decision generation.
- `fastapi/app/schemas/chat.py` defines `TopicPolicyDecision` and `TopicPolicyDecisionReport` as the contract for ranking outcomes.
- `fastapi/app/routers/chat.py` exposes `/chat/topic-policy-decisions` as the public entry point for the policy layer.
- The next adjacent expansion should add a room-assignment contract only if the booking service becomes the actual owner of placement logic.

### 8.12.2 Room-assignment gap anchor

- `fastapi/app/services/bookings.py` does not yet expose a room-assignment or matching helper that can own placement rules.
- `fastapi/app/routers/bookings.py` therefore remains a lifecycle router rather than a room-routing surface.
- `fastapi/app/schemas/schemas.py` has no explicit room-assignment contract yet, so any room model would still be speculative.
- The next concrete expansion beyond topic policy should be a schema-first room-assignment slice only after the booking service gains a deterministic matching helper.
- The first room-assignment schema should carry at least the room identifier, the matched customer, the match reason, and the placement state so the decision is replayable.
- Until that schema exists, any room-selection language should stay in the backlog rather than in the public route map.

### 8.12.3 Booking contract boundary

- The current booking contract now includes lifecycle, event history, and a narrow assignment report, so room-oriented extensions still need new persisted fields or a new schema type before they become real placement behavior.
- The booking assignment helper already lives in `fastapi/app/services/bookings.py`, but it is still deterministic and context-derived rather than a persisted matching engine.
- Until room data exists, the safest next slice is still metadata or schema scaffolding, not a wider public booking route.
- The booking router should only gain a placement route after a schema can describe suggested, accepted, rejected, expired, or reassigned placement states.
- A booking assignment decision should include the source of the match and the reason it was accepted or rejected so the lifecycle trail stays explainable.

### 8.12.4 Topic-selection bridge

- `fastapi/app/services/chat_analytics.py` is the current owner for topic ranking and policy decisions, so the next topic-adjacent slice should remain there unless a new domain owner is introduced.
- `fastapi/app/schemas/chat.py` already carries the topic decision contracts, which makes it the correct boundary for any new topic-selection payloads.
- `fastapi/app/routers/chat.py` should continue to act as the thin transport layer for these decision outputs instead of recreating selection logic.
- Any future room or topic assignment surface should be added only after the service layer proves the decision rules are deterministic and reproducible.
- The chat router already has a concrete `/chat/topic-ranking` path, so any later topic-selection addition should be a sibling of that route rather than a replacement for it.
- A future topic-selection payload should express ranking inputs, selected topic, and explanation fields so the service output stays auditable.
- A topic-selection helper should return both the chosen topic and the alternatives it considered so the router can explain why a given topic won.
- The policy layer should remain separate from the ranking layer so topic selection does not silently collapse into policy enforcement.
- Any downstream client that consumes topic decisions should treat `TopicPolicyDecisionReport` as the stable contract, not the raw service internals.

### 8.13 Near-term implementation slice

- Keep the chat analytics helper shared between service and router code so insight generation has a single owner.
- Use the current recovery and retention contracts as the template for any new room or topic decision payload.
- Add topic-selection logic next if the room allocation work is still only conceptual.
- Add the first room-assignment schema only after the assignment rules are stable in the service layer.
- Extend observability only after the matching and policy decisions can be reproduced from stored inputs.

### 8.14 Next revision focus

- The chat analytics helper split is complete: `fastapi/app/routers/chat.py` now consumes `fastapi/app/services/chat_analytics.py` for interaction insights instead of maintaining a router-local copy.
- The first topic-ranking contract is now real: `fastapi/app/schemas/chat.py` defines `TopicRankingItem` and `TopicRankingReport`, `fastapi/app/services/chat_analytics.py` builds the report, and `fastapi/app/routers/chat.py` exposes it through `/chat/topic-ranking`.
- The topic-policy contract is now also real: `fastapi/app/schemas/chat.py` defines `TopicPolicyDecision` and `TopicPolicyDecisionReport`, `fastapi/app/services/chat_analytics.py` builds the decision set, and `fastapi/app/routers/chat.py` exposes it through `/chat/topic-policy-decisions`.
- Booking assignment is now a concrete backend slice: the booking service owns a deterministic assignment helper, the schema can accept caller-provided assignment details, and the router can persist and update them through dedicated endpoints.
- The booking contract itself still ends at lifecycle, event history, and assignment lifecycle tracking, with the latest assignment marked explicitly current, so real room assignment should still wait for persisted placement fields.
- Define the first topic-selection payload and decide whether it belongs in chat analytics or a dedicated booking matcher.
- Topic selection now has a concrete owner in chat analytics until a room-assignment matcher proves it needs to move.
- Capture the room-assignment contract in schema form before any new router path is added.
- Add one metadata endpoint or summary field that exposes the new decision surface without requiring internal debugging.
- Keep the remaining backlog items grouped by service owner so the next revision can expand one path at a time.

### 8.15 Revision closure rule

- Treat the current backlog as closed for this revision once the next implementation slice is selected and the owning service is unambiguous.
- Avoid adding new backlog breadth in the same revision unless it directly depends on the selected slice.
- Preserve the current section order so the next revision can append detail without reshaping the document.
- Reopen only the narrowest adjacent gap needed for the chosen implementation path.

### 8.16 Current implementation checkpoint

- The chat analytics service is now the canonical owner for interaction insight generation.
- The router should remain a request-and-response adapter around the shared service output.
- The next concrete expansion should add one structured room or policy contract rather than broadening the topic surface further.
- The booking service now includes a deterministic assignment helper and a persisted assignment record path, but it still stops short of a persisted room-assignment abstraction.
- Topic policy is the last implemented decision surface in chat before the map should shift to a new backend owner.
- The current revision should not invent a room-assignment abstraction until `fastapi/app/services/bookings.py` grows a real matching helper.
- If more beyond this document is needed, the next revision should start from a concrete service implementation rather than a new planning branch.

### 8.17 Traceability addendum

- Room allocation remains a future booking-service concern until a real assignment helper exists in `fastapi/app/services/bookings.py`.
- Topic selection remains a chat-analytics concern until the service emits a dedicated topic payload that the router consumes.
- Authenticity and legal filtering remain policy-layer concerns until schema contracts can separate allowed, restricted, review-required, and blocked outcomes.
- Observability remains a main-app concern until `fastapi/app/main.py` surfaces the new decision path in capability or ecosystem reporting.
- Human review remains a shared concern across routers, services, and schemas until the handoff states are explicitly modeled.
- Booking placement, if it ever lands, should be traced from service helper to schema contract before any router route is added.
- The current map should now treat chat-policy and booking-lifecycle as the last fully described implemented boundaries for this revision.
- Interoperability should treat the schema layer as the only stable handoff point for any new room, topic, policy, or review decision.

### 8.18 Finalization note

- No additional backlog breadth should be appended to this revision unless it directly resolves one of the traceability items above.
- Any future expansion should begin with the owning service, then the schema, then the router, then metadata.
- The current document is therefore complete as a planning artifact for revision `r4`.
- Any further work here should start a new revision only after a concrete backend owner is added to the implementation.
- If the user still wants "more beyond," the next revision should be a concrete implementation slice, not another planning-only expansion.
- The current revision has reached the point where any extra detail would need real backend code behind it.
- The map is now intentionally capped at the current code-backed owners so the next step can be implementation rather than elaboration.

## 8. Evidence Index

This section is the stable entry point for future revision passes.

### Backend anchors

- [fastapi/app/main.py](fastapi/app/main.py)
- [fastapi/app/models.py](fastapi/app/models.py)
- [fastapi/app/routers/users.py](fastapi/app/routers/users.py)
- [fastapi/app/routers/bookings.py](fastapi/app/routers/bookings.py)
- [fastapi/app/routers/chat.py](fastapi/app/routers/chat.py)

### Requirement anchors

- [requirement/core_structure.json](requirement/core_structure.json)
- [requirement/services_and_planning.json](requirement/services_and_planning.json)
- [requirement/multilingual_and_testing.json](requirement/multilingual_and_testing.json)

### Likely next files

- [fastapi/app/models.py](fastapi/app/models.py) for any new persisted room, topic, policy, or review entities.
- [fastapi/app/services/bookings.py](fastapi/app/services/bookings.py) for assignment, reassignment, and lifecycle helpers.
- [fastapi/app/services/chat_analytics.py](fastapi/app/services/chat_analytics.py) for topic selection, recovery, and explanation logic.
- [fastapi/app/services/retention.py](fastapi/app/services/retention.py) for snapshotting and longitudinal reporting behavior.
- [fastapi/app/schemas/schemas.py](fastapi/app/schemas/schemas.py) for shared request and response contracts.
- [fastapi/app/schemas/chat.py](fastapi/app/schemas/chat.py) for recovery, retention, and operations payloads.
- [fastapi/app/routers/bookings.py](fastapi/app/routers/bookings.py) for public lifecycle and assignment endpoints.
- [fastapi/app/routers/chat.py](fastapi/app/routers/chat.py) for chat-derived intelligence and operational reporting endpoints.
- [fastapi/app/routers/users.py](fastapi/app/routers/users.py) for consent, identity, and admin-boundary changes.
- [fastapi/app/main.py](fastapi/app/main.py) for metadata, capability, and ecosystem reporting.

## 9. Revision Rules

Use the same document shape for future updates:

1. Update the executive summary only if the architecture changes materially.
2. Preserve the layered order from shell to gaps.
3. Add new facts under the relevant layer instead of rewriting the entire document.
4. Keep gap items explicit until they are verified resolved.
5. Add a new revision ID at the top when the document is substantially reworked.

## 10. Current Assessment

The repository is functionally a backend-first customer-service platform with an underlayer of analytics, retention intelligence, and a broad operational governance layer. The product story is coherent, but the implementation is not perfectly unified yet. The strongest conceptual boundary is the backend domain model, while the weakest boundary is the mismatch between requirements and the currently implemented backend contract.

## 11. Backend Expansion Todos

- Add system-only and customer-point ranking metrics as the basis for all backend control surfaces.
- Add customer access controls that depend on system metrics or customer-point rankings instead of ad hoc rules.
- Add customer interest scoring for topics, rooms, and communities using a simple monitored score space.
- Add proximity scoring between users and between communities so routing can stay explainable.
- Add AI-generated system metrics where agents need to contribute their own observable signals.
- Add ranking-based controls for functionality access, topic selection, and routing decisions.
- Add monitoring-friendly improvement and feedback workflow hooks for every new scoring surface.
- Add room allocation, topic ranking, authenticity filtering, legal filtering, governance, observability, privacy, and incident-response todos only as they depend on the score-based control model.

## 12. Backend Guardrail Todos

- Keep every control decision grounded in either system-only metrics or customer-centric point rankings.
- Keep AI-generated metrics visible as system metrics rather than hidden control inputs.
- Keep functionality access, topic interest, user proximity, and community proximity explainable through the same simple score model.
- Keep the score model simple enough for improvement and feedback workflows to inspect directly.
- Keep backend decisions reproducible from stored metrics, ranking inputs, and rule versions.
- Keep routing, moderation, and policy behavior auditable when metric sources disagree.
- Keep access, placement, and prioritization logic separate from presentation logic.
- Keep future room, topic, policy, privacy, and incident features aligned with the same score-based control boundary.
