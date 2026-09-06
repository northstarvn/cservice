# CService Concept Map

## Revision Control

- Revision ID: `r0`
- Scope: repository-wide conceptual model for the current workspace state
- Purpose: provide a stable, top-down structure that can be revised repeatedly without changing the document's shape
- Reading rule: each layer reveals only the next level of detail; missing pieces are listed explicitly as gaps instead of being inferred

## 1. Executive View

This repository is a customer-service product split across a React/Vite frontend and a FastAPI backend. The system presents service booking, chat support, tracking, profile management, and project-planning experiences, while the backend also exposes deeper analytics, retention, lifecycle, and ecosystem metadata that are only partially surfaced in the UI.

The implementation is best understood as three overlapping systems:

1. Customer-facing experience layer
2. Account, booking, and conversation state layer
3. Analytics, retention, and operational insight layer

The current workspace contains both implemented surfaces and requirement artifacts that describe a broader intended product. The document therefore distinguishes between:

- Implemented behavior
- Intended behavior from requirement files
- Gaps, mismatches, or duplicated concepts

## 2. Top-Level System Shape

### 2.1 Frontend

The frontend is a React application built with Vite and styled primarily through Tailwind utility classes plus a shared CSS file. It routes through a single app shell and uses context providers for auth, app state, and chat state.

Primary entry chain:

- `src/main.jsx`
- `src/App.jsx`
- `src/context/AuthContext.jsx`
- `src/context/AppContext.jsx`
- `src/context/ChatContext.jsx`
- `src/pages/*`

### 2.2 Backend

The backend is a FastAPI application with async SQLAlchemy, token-based auth, and routers for users, bookings, and chat. It also exposes metadata and capability endpoints that describe a richer ecosystem than the visible UI currently uses.

Primary entry chain:

- `fastapi/app/main.py`
- `fastapi/app/db.py`
- `fastapi/app/models.py`
- `fastapi/app/routers/users.py`
- `fastapi/app/routers/bookings.py`
- `fastapi/app/routers/chat.py`

### 2.3 Requirement Layer

The `requirement/` folder acts like a product brief and intended architecture archive. It describes menus, screens, multilingual behavior, testing expectations, and a service-planning flow that does not fully match the current app routing.

Key artifacts:

- `requirement/core_structure.json`
- `requirement/services_and_planning.json`
- `requirement/multilingual_and_testing.json`
- `requirement/security_spec.js`
- `requirement/mobile_spec.js`
- `requirement/tracking_rtl_spec.js`

## 3. Frontend Concept Model

### 3.1 Shell and Boot

The application boots from `src/main.jsx`, which mounts `App`. `App` wraps the app in error handling and three context providers, then places the router around the interactive content.

At this level, the shell is responsible for:

- Establishing app-wide state
- Supplying auth state
- Supplying chat state
- Rendering the persistent navbar
- Mounting route-driven screens
- Managing login, signup, and booking popups

### 3.2 Context Layer

#### Auth Context

`src/context/AuthContext.jsx` owns session validation, login, registration, logout, and profile update state. It stores the current user and token lifecycle behavior in local storage-backed flows.

Conceptually, it is the source of truth for:

- Is the user authenticated?
- Who is the current user?
- Is the app still validating a token?
- How does the app log in or out?

#### App Context

`src/context/AppContext.jsx` owns global UX concerns such as language, theme, mobile detection, notifications, loading, analytics counters, and popup state.

Conceptually, it is the source of truth for:

- Which language is active?
- Which theme is active?
- Which modal or popup is open?
- Is the UI in a loading state?
- What lightweight analytics events are being tracked?

#### Chat Context

`src/context/ChatContext.jsx` owns the conversational workflow: message history, chat submission, suggestions, sentiment, voice mode, and language selection.

Conceptually, it is the source of truth for:

- What messages have been exchanged?
- Is chat loading?
- What suggested replies exist?
- Is voice mode active?
- What language should speech recognition use?

### 3.3 Route Layer

Current React routes in `src/App.jsx` are:

- `/` -> Home
- `/chat` -> Chat
- `/booking` -> Booking
- `/bookings` -> MyBookings
- `/tracking` -> Tracking
- `/profile` -> Profile
- `/planning` -> Planning
- `/login` -> Login redirect flow

The route list is stable enough to describe the current UI map, but it does not fully match the paths described in the requirement folder.

### 3.4 Screen Layer

#### Home

The home page is a marketing and navigation hub. It promotes four core actions:

- AI chat
- Booking
- Tracking
- Project planning

It also shows recent bookings for authenticated users.

#### Chat

The chat page is the conversational support surface. It displays messages, suggestions, sentiment, and an optional voice-recognition mode.

#### Booking

The booking page is the transactional scheduling surface. It supports creation, editing, deletion, filtering, and pagination for user bookings.

#### Tracking

The tracking page is a shipment/delivery lookup surface. It uses a mock tracking flow and displays a result popup.

#### Profile

The profile page is the account management surface. It allows editing display name, email, and preferred language, plus logout and a few auxiliary account actions.

#### Planning

The planning page is an idea-capture and AI-suggestion surface. It accepts project name and requirements, then generates heuristic suggestions.

### 3.5 Component Layer

Reusable components fall into a few groups:

- Navigation and global UI: `Navbar`, `NotificationContainer`, `LoadingSpinner`, `LoadingSkeletons`, `ErrorBoundary`
- Authentication popups: `LoginPopup`, `SignupPopup`
- Booking overlays: `BookingConfirmationPopup`
- Tracking overlays: `TrackingResultPopup`
- Support widgets: `Popup`, `DebugAuth`

The component set suggests a modal-heavy product with shared chrome and multiple task-specific surfaces.

### 3.6 Style Layer

The visual system is not centralized in one design system file. It combines Tailwind utility classes, a global stylesheet in `src/styles/App.css`, and some component-local inline styles. This makes the app visually functional but conceptually fragmented.

## 4. Backend Concept Model

### 4.1 Startup and Environment

`fastapi/app/main.py` constructs the app, configures CORS, verifies the database on startup, and registers exception handling. It exposes metadata endpoints that describe the app, capabilities, ecosystem status, and authenticated probe routes.

At a conceptual level, the backend startup owns:

- App identity and version
- Database readiness
- Cross-origin access policy
- Global error shaping
- Router registration

### 4.2 Domain Model

`fastapi/app/models.py` defines the persistent business objects:

- `User`
- `Booking`
- `BookingEvent`
- `ChatHistory`
- `InteractionSignal`
- `RetentionSnapshot`

These objects indicate that the backend is not just a CRUD booking service. It is also designed to preserve interaction signals, audit booking transitions, and track retention health over time.

### 4.3 User and Auth API

`fastapi/app/routers/users.py` supports registration, login, current-user lookup, and password change. This is the account control plane for the app.

### 4.4 Booking API

`fastapi/app/routers/bookings.py` supports booking creation, listing, reading, editing, deleting, and audit/history summaries. It also exposes admin-style summary analytics.

This router is the clearest implementation of a lifecycle model rather than a simple booking form.

### 4.5 Chat API

`fastapi/app/routers/chat.py` is the broadest surface. It includes chat history, sentiment analysis, interaction insights, retention dashboards, maintenance reports, risk profiles, system priorities, monetization cohorts, and operational readiness views.

The chat router is therefore both a conversation API and a business-intelligence API.

## 5. Requirement-Layer Intent

The requirement files describe a more formal customer-service product language than the current routing suggests.

### 5.1 Intended Navigation

`requirement/core_structure.json` describes a menu centered on:

- Home
- AI Assistant
- Services
- Project Planning
- Profile
- Language switching

This is a useful conceptual map even where the current UI uses different paths or names.

### 5.2 Intended Screens

The requirement set expects:

- A home screen with AI chat and service cards
- A dedicated AI chat screen
- A services booking screen
- A project-planning screen with AI-generated requirements
- A language selector with localized content
- A testing strategy around these user journeys

### 5.3 Intended Multilingual Model

The multilingual spec expects language files and selector-driven switching among at least English, Spanish, and French. Current implementation has language flags and switching, but translation loading is still partial and localized content is not yet fully centralized.

## 6. Layered Walkthrough From User Intent to Data

### Layer 1: User goal

A user arrives wanting support, a booking, tracking, or help planning a project.

### Layer 2: Navigation choice

The user picks a route or popup via the navbar, home page, or login flow.

### Layer 3: Context selection

The app resolves auth, language, theme, and modal state through contexts.

### Layer 4: Screen behavior

The visible page renders the requested task surface and may invoke API calls.

### Layer 5: Backend exchange

The frontend talks to FastAPI for identity, bookings, chat, and history.

### Layer 6: Persistence and analytics

The backend stores the business object or history item, computes summary outputs, and can expose analytic or retention views.

### Layer 7: Operational insight

The metadata endpoints and retention/chat analytics present the same product from an operational viewpoint rather than a user-facing one.

## 7. Stable Gap Register

These are the main gaps that should remain visible across revisions.

### 7.1 Route mismatch

The requirement files describe `/ai-chat`, `/services`, and `/project-planning`, while the current app uses `/chat`, `/booking`, and `/planning`.

### 7.2 Duplicate conceptual flows

The codebase contains overlapping implementations for some ideas, especially around chat, planning, booking, and translation. Some paths are context-driven and some are page-driven, which suggests partial migration or parallel prototypes.

### 7.3 Incomplete app-context contract

Some pages expect helpers such as notification and loading setters that are not clearly present in the current context contract. This is a likely source of drift.

### 7.4 Chat integration uncertainty

The chat UI and chat context expect auth token plumbing and endpoints that need careful verification against the backend contract.

### 7.5 Translation system fragmentation

There are multiple i18n-related files and references, but translation loading and language ownership are not yet centralized.

### 7.6 Backend breadth exceeds frontend surface

The backend already exposes rich retention and ecosystem endpoints, but the visible UI does not yet map to most of them.

## 8. Evidence Index

This section is the stable entry point for future revision passes.

### Frontend anchors

- [src/App.jsx](src/App.jsx)
- [src/main.jsx](src/main.jsx)
- [src/context/AuthContext.jsx](src/context/AuthContext.jsx)
- [src/context/AppContext.jsx](src/context/AppContext.jsx)
- [src/context/ChatContext.jsx](src/context/ChatContext.jsx)
- [src/pages/Home.jsx](src/pages/Home.jsx)
- [src/pages/Booking.jsx](src/pages/Booking.jsx)
- [src/pages/Chat.jsx](src/pages/Chat.jsx)
- [src/pages/Planning.jsx](src/pages/Planning.jsx)
- [src/pages/Tracking.jsx](src/pages/Tracking.jsx)
- [src/pages/Profile.jsx](src/pages/Profile.jsx)

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

## 9. Revision Rules

Use the same document shape for future updates:

1. Update the executive summary only if the architecture changes materially.
2. Preserve the layered order from shell to gaps.
3. Add new facts under the relevant layer instead of rewriting the entire document.
4. Keep gap items explicit until they are verified resolved.
5. Add a new revision ID at the top when the document is substantially reworked.

## 10. Current Assessment

The repository is functionally a customer-service platform with an underlayer of analytics and retention intelligence. The product story is coherent, but the implementation is not perfectly unified yet. The strongest conceptual boundary is the backend domain model, while the weakest boundary is the mismatch between requirements, frontend route names, and context contracts.

## 11. Expansion Priorities

The following expansion areas are ordered by expected impact on customer loyalty, repeat usage, and dissatisfaction reduction over time. The goal is to make the platform more habit-forming in a product sense by increasing perceived usefulness, reducing friction, and closing unresolved loops faster.

### 11.1 Priority 1: Dissatisfaction Prevention Loop

Build a closed-loop dissatisfaction system that detects friction early, records the cause, and routes it into a visible recovery path.

Target outcomes:

- Fewer unresolved negative experiences
- Faster response to repeated complaints
- Better visibility into recurring pain points
- Lower churn caused by avoidable frustration

Suggested implementation layers:

- Frontend: lightweight complaint capture, contextual feedback prompts, and visible status updates
- Backend: sentiment and interaction signal aggregation, escalation routing, and follow-up state tracking
- Metrics: complaint recurrence, time-to-recovery, and unresolved issue rate

### 11.2 Priority 2: Loyalty and Repeat-Use Design

Increase repeat engagement by making the platform feel progressively more useful the longer someone uses it.

Target outcomes:

- More return visits
- Higher completion rate for core journeys
- Greater trust in the platform as a default support destination
- Higher retention among users with prior unresolved issues

Suggested implementation layers:

- Frontend: personalized recent activity, saved preferences, and proactive next-step suggestions
- Backend: user journey history, repeat-intent detection, and loyalty scoring based on resolved interactions
- Metrics: repeat session rate, retention cohorts, and task re-entry after completion

### 11.3 Priority 3: Friction Removal and Confidence Building

Reduce avoidable uncertainty in booking, chat, tracking, and profile workflows so users complete actions with less hesitation.

Target outcomes:

- Lower abandonment across key flows
- More confident first-time usage
- Reduced support load from simple confusion
- Better perceived reliability

Suggested implementation layers:

- Frontend: clearer empty states, stronger inline guidance, and fewer ambiguous labels
- Backend: validation messages that are specific and actionable
- Metrics: form drop-off, retry frequency, and error recovery rate

### 11.4 Priority 4: Personalized Re-engagement

Use past behavior to re-engage users with the next most relevant action instead of a generic landing experience.

Target outcomes:

- More useful home-page personalization
- Better follow-up after resolved issues or bookings
- Increased click-through to high-value features
- Better continuity across visits

Suggested implementation layers:

- Frontend: personalized cards, reminders, and contextual shortcuts
- Backend: user-level summaries, event-based triggers, and recommendation signals
- Metrics: personalized CTA conversion, reminder response rate, and repeat task completion

### 11.5 Priority 5: Trust Accumulation Over Time

Make trust visible through stable history, explainable decisions, and consistent follow-through.

Target outcomes:

- Stronger long-term confidence in the service
- Lower perceived risk when making a booking or starting a chat
- Better acceptance of suggestions and recovery actions
- More positive retention sentiment

Suggested implementation layers:

- Frontend: transparent status indicators and history views
- Backend: audit trails, booking events, and retention snapshots
- Metrics: support satisfaction, resolved-case confidence, and return-after-resolution rate

## 12. Expansion Guardrails

These priorities should improve loyalty without creating unhealthy dependency or obscuring user control.

### 12.1 Supportive engagement, not coercive engagement

The product should encourage return use by being useful, reliable, and personalized. It should not rely on manipulative patterns that pressure users into repeated visits.

### 12.2 Dissatisfaction must be measurable

Every recovery flow should produce a traceable signal so the team can see whether dissatisfaction is trending down over time.

### 12.3 Loyalty must remain linked to value

Repeat use should come from faster resolution, better outcomes, and reduced effort, not from friction that traps the user in the system.

### 12.4 Recovery should shorten future effort

A resolved problem should make the next interaction easier by saving context, learning preferences, or improving defaults.

### 12.5 Expansion should stay revision-friendly

New loyalty features should be added as sublayers beneath this section, not by rewriting the earlier architecture map.
