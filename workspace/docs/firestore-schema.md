# Firestore Central Database Schema

Purpose: define the central Firestore data model for Agent Genaie/OpenClaw services.

The user signs in once with Google/Firebase Auth. That login creates the canonical app user. Every service subscription, credential reference, job application, Gmail connection, and news preference attaches to that same user record.

Firestore is the operational database. Firebase Auth remains the authentication provider. Credential storage is pluggable and must not be tied to Passbolt.

Implementation status on 2026-06-03: Agent Genaie now writes Firebase users and Gmail connections to this central schema. Gmail sending still reads the existing local encrypted token store while Firestore is used as the central mirror/backfill layer.

## Design Rules

- Use Firebase UID as the internal `userId` whenever the user has signed in to the app.
- Use `publicUserId` for user-facing URLs. Do not put Firebase UID, email, phone number, or service-specific user IDs in public links.
- Do not ask a signed-in user for name or email again. Read these from `users/{userId}.profile`.
- Store service-specific choices in subscription/service documents, not repeated user identity fields.
- If a service needs credentials or external account access, create a `credentialRef` linked to the same `userId`.
- Do not make Gmail, Job Scout, or News code depend directly on Passbolt. They should depend on `credentialRefs` and a provider adapter.
- Never store plaintext passwords, OAuth tokens, API keys, recovery codes, or private keys in Firestore.
- Secret material may be stored only as encrypted blobs or by reference to an external vault provider.

## Identity

### `users/{userId}`

Canonical user profile. `userId` is the Firebase UID.

```ts
{
  userId: string,
  publicUserId: string,
  createdAt: Timestamp,
  updatedAt: Timestamp,

  profile: {
    email: string,
    emailVerified: boolean,
    displayName: string | null,
    photoUrl: string | null,
    firstName: string | null,
    lastName: string | null
  },

  identities: {
    firebaseUid: string,
    googleProviderUid: string | null,
    whatsappPhoneHash?: string,
    whatsappPhone?: string | null
  },

  serviceStatus: {
    gmail: "not_connected" | "connected" | "revoked",
    jobs: "not_subscribed" | "active" | "paused" | "disabled",
    news: "not_subscribed" | "active" | "paused" | "disabled"
  }
}
```

### `publicUsers/{publicUserId}`

Reverse lookup for user-facing URLs. `publicUserId` is an opaque app id such as `usr_q8K2mV7xPz4aTn9B`.

```ts
{
  publicUserId: string,
  firebaseUid: string,
  createdAt: Timestamp,
  updatedAt: Timestamp
}
```

## Service Subscriptions

### `serviceSubscriptions/{subscriptionId}`

Generic subscription record. Use deterministic ID: `${userId}_${service}`.

```ts
{
  subscriptionId: string,
  userId: string,
  service: "jobs" | "news" | "gmail" | string,
  status: "pending" | "active" | "paused" | "disabled",
  createdAt: Timestamp,
  updatedAt: Timestamp,

  preferences: object,
  metadata: object
}
```

Job Scout example:

```ts
{
  subscriptionId: "<userId>_jobs",
  userId: "<userId>",
  service: "jobs",
  status: "active",
  preferences: {
    targetRoles: string[],
    locations: string[],
    qualifications: string[],
    experience: string[],
    skills: string[],
    exclusions: string[],
    promptNotes: string[],
    autoApply: boolean,
    maxApplicationsPerRun: number
  },
  metadata: {
    whatsappPhone: string,
    whatsappPhoneHash: string,
    senderEmail: "<users.profile.email unless user chooses another connected sender>",
    cvFileRef: string | null,  // R2 object key "<uid>/cv/cv.pdf" (not a local path)
    setupStatus: "linked" | "profile_saved" | "ready"
  }
}
```

## Pluggable Credential Vault

### `credentialRefs/{credentialRefId}`

Provider-independent credential reference. Services read credentials through a provider adapter using this document.

```ts
{
  credentialRefId: string,
  userId: string,
  service: "gmail" | "jobs" | string,
  purpose: "oauth_token" | "username_password" | "api_key" | "session" | string,

  provider: {
    type: "passbolt" | "infisical" | "firestore_encrypted" | "custom_vault",
    ref: string,
    project: string | null,
    environment: string | null
  },

  status: "pending" | "ready" | "revoked" | "disabled" | "failed",
  encryptedSecret: EncryptedSecretBlob | null,
  metadata: object,

  createdAt: Timestamp,
  updatedAt: Timestamp,
  lastVerifiedAt: Timestamp | null,
  revokedAt: Timestamp | null
}
```

Provider reference examples:

```ts
// Passbolt
{
  type: "passbolt",
  ref: "resource:<passbolt-resource-id>",
  project: null,
  environment: null
}

// Infisical
{
  type: "infisical",
  ref: "secretPath:/users/<userId>/jobs",
  project: "genaie",
  environment: "prod"
}

// Firestore encrypted
{
  type: "firestore_encrypted",
  ref: "credentialRefs/<credentialRefId>",
  project: "firebase-project-id",
  environment: "prod"
}

// Custom vault
{
  type: "custom_vault",
  ref: "vault://users/<userId>/jobs",
  project: "genaie",
  environment: "prod"
}
```

### `credentialRequests/{requestId}`

Tracks user-facing setup requests for credentials or OAuth permissions.

```ts
{
  requestId: string,
  userId: string,
  credentialRefId: string,
  service: "gmail" | "jobs" | string,
  channel: "whatsapp" | "web" | "admin",
  status: "created" | "sent" | "accepted" | "completed" | "expired" | "failed",
  setupUrl: string | null,
  provider: "passbolt" | "infisical" | "firestore_encrypted" | "custom_vault",
  createdAt: Timestamp,
  updatedAt: Timestamp,
  expiresAt: Timestamp | null
}
```

### `EncryptedSecretBlob`

Used only when `provider.type === "firestore_encrypted"` or for encrypted fallback copies.

```ts
type EncryptedSecretBlob = {
  alg: "aes-256-gcm",
  keyVersion: string,
  iv: string,
  tag: string,
  ciphertext: string,
  aad: string,
  updatedAt: Timestamp
}
```

Provider adapter interface:

```ts
credentialProvider.getSecret(credentialRefId)
credentialProvider.createRequest(userId, service, schema)
credentialProvider.markReady(credentialRefId)
credentialProvider.revoke(credentialRefId)
```

## Gmail

### `gmailConnections/{userId}`

Gmail send permission for the same app user. Google login provides identity; Gmail OAuth provides send permission.

```ts
{
  userId: string,
  firebaseUid: string,
  senderEmail: string,
  connected: boolean,
  scope: "https://www.googleapis.com/auth/gmail.send",
  credentialRefId: string,
  connectedAt: Timestamp,
  updatedAt: Timestamp,
  revokedAt: Timestamp | null
}
```

Rules:

- `senderEmail` should normally match `users/{userId}.profile.email`.
- Job Scout can use `users/{userId}.profile.email` automatically for applicant identity.
- Job Scout can send from Gmail only when `gmailConnections/{userId}.connected === true`.
- The Gmail OAuth token is stored through `credentialRefs`, not directly in service code.

## Personalized News

### `newsSubscriptions/{userId}`

```ts
{
  userId: string,
  enabled: boolean,
  topics: string[],
  frequency: string,
  language: string,
  createdAt: Timestamp,
  updatedAt: Timestamp,
  lastSentAt: Timestamp | null,
  lastStatus: "delivered" | "failed" | "skipped" | null,
  lastMessage: string | null
}
```

## Job Scout

### `jobSetupLinks/{tokenHash}`

Short-lived WhatsApp-to-login binding links created by OpenClaw and consumed by Agent Genaie after Firebase login.

```ts
{
  tokenHash: string,
  service: "jobs",
  channel: "whatsapp",
  phone: string,
  phoneHash: string,
  status: "created" | "used" | "expired" | "revoked",
  createdAt: Timestamp,
  updatedAt: Timestamp,
  expiresAt: Timestamp,
  usedAt: Timestamp | null,
  usedBy: string | null
}
```

Rules:

- The raw setup token appears only in the user-facing setup URL.
- Store only the HMAC/token hash in Firestore.
- The setup route binds `users/{userId}.identities.whatsappPhoneHash` and `whatsappPhone`.
- Public links must not expose Firebase UID or email.

### `jobApplications/{applicationId}`

Use deterministic `applicationId = sha256(userId + company + role)` to prevent duplicate applications for the same user/company/role.

```ts
{
  applicationId: string,
  userId: string,
  subscriptionId: string,

  applicant: {
    email: string,
    displayName: string | null
  },

  sender: {
    email: string,
    gmailConnectionId: string | null
  },

  company: string,
  role: string,
  applicationEmail: string | null,
  source: string,
  status: "applied" | "skipped" | "action_required" | "physical_submission" | "failed",
  submissionMethod: "email" | "website" | "manual" | null,
  applicationUrl: string | null,
  blockerCode: string | null,
  attemptCount: number,
  lastAttemptAt: Timestamp | null,
  evidence: {
    finalUrl: string | null,
    filledFields: string[]
  },
  appliedAt: Timestamp | null,
  createdAt: Timestamp,
  updatedAt: Timestamp,
  notes: string | null
}
```

Rules:

- Applicant email/name come from `users/{userId}.profile` at apply time.
- Do not ask a signed-in user for a separate application email.
- If sending email, use `gmailConnections/{userId}` or another explicitly connected sender.
- Set `applied` only after Gmail returns a message ID or the browser worker detects a submission confirmation.
- Use `action_required` for login, CAPTCHA, unknown required questions, declarations, payment requests, and unsupported forms. `physical_submission` is retained only for historical compatibility.
- Retry only `failed` records, up to `attemptCount === 3`; all successful, skipped, historical, and action-required records are terminal until an operator explicitly replaces them.

## Cron

### `cronJobs/{jobId}`

```ts
{
  jobId: string,
  name: string,
  enabled: boolean,
  schedule: {
    kind: "cron",
    expr: string,
    tz: string
  },
  payload: object,
  createdAt: Timestamp,
  updatedAt: Timestamp
}
```

### `cronJobState/{jobId}`

```ts
{
  jobId: string,
  nextRunAt: Timestamp | null,
  lastRunAt: Timestamp | null,
  lastRunStatus: "ok" | "failed" | "skipped" | null,
  lastDurationMs: number | null,
  lastDeliveryStatus: string | null,
  consecutiveErrors: number,
  consecutiveSkipped: number,
  updatedAt: Timestamp
}
```

### `cronRuns/{runId}`

```ts
{
  runId: string,
  jobId: string,
  startedAt: Timestamp,
  finishedAt: Timestamp | null,
  status: "ok" | "failed" | "skipped",
  durationMs: number | null,
  summary: string | null,
  deliveryStatus: string | null,
  error: string | null
}
```

## Required Indexes

```text
users.profile.email ASC
users.publicUserId ASC
users.identities.firebaseUid ASC
users.identities.whatsappPhoneHash ASC

publicUsers.firebaseUid ASC

serviceSubscriptions.userId ASC
serviceSubscriptions.service ASC
serviceSubscriptions.status ASC
serviceSubscriptions.userId ASC, serviceSubscriptions.service ASC

credentialRefs.userId ASC
credentialRefs.service ASC
credentialRefs.provider.type ASC
credentialRefs.status ASC
credentialRefs.userId ASC, credentialRefs.service ASC

credentialRequests.userId ASC
credentialRequests.status ASC
credentialRequests.expiresAt ASC

gmailConnections.senderEmail ASC
gmailConnections.connected ASC
gmailConnections.userId ASC

newsSubscriptions.enabled ASC
newsSubscriptions.userId ASC

jobSetupLinks.status ASC
jobSetupLinks.expiresAt ASC
jobSetupLinks.phoneHash ASC

jobApplications.userId ASC, company ASC, role ASC
jobApplications.userId ASC, status ASC

cronJobs.enabled ASC
cronJobState.nextRunAt ASC
cronRuns.jobId ASC, startedAt DESC
```

## Migration Sources

Current local sources to migrate:

- Agent Genaie Gmail tokens: `/home/joseph/projects/agent_genaie/data/tokens.json`
- Public news subscriptions: `/home/joseph/.openclaw/runtime/public-news/subscriptions.json`
- Job tracker: `/home/joseph/.openclaw/workspace/jobs/tracker.json`
- Cron jobs/state/runs: `/home/joseph/.openclaw/cron/jobs.json`, `jobs-state.json`, `runs/*.jsonl`
- Passbolt: migrate only operational references into `credentialRefs`; do not make Firestore schema depend on Passbolt tables.

## Future Service Rule

Any new service must follow this rule:

```text
Do not ask for email/name again if the user is already signed in.
Read identity from users/{userId}.profile.
Store only service-specific preferences in the service subscription.
```

If a service needs credentials or external access, create a `credentialRef` linked to the same `userId`.
