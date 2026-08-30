# What "verified" can and cannot mean

Commercial providers sell "verified" addresses. It is worth understanding
precisely what that word can carry, because most of it is not available to you
for free — and some of it is not available to anyone.

## What ReachOut checks for free

| Check | What it proves | What it does not prove |
|---|---|---|
| Syntax | the string could be an address | nothing about the mailbox |
| Domain resolves | the domain exists | nothing about mail |
| MX records | the domain accepts mail somewhere | nothing about this mailbox |
| Provider fingerprint | who runs the mail (Google, Microsoft, Proofpoint…) | — |
| Role detection | `info@`, `sales@` reach a team | — |
| Disposable detection | throwaway domain | — |
| Pattern match | the format fits how this company builds addresses | that this person exists |

None of these establishes that mail sent to the address will arrive. ReachOut
therefore never reports "verified" — it reports which checks passed.

## SMTP probing, and why it is off by default

You can open an SMTP conversation with a domain's mail server and ask whether it
would accept a recipient, without sending anything:

```
MAIL FROM:<you@example.com>
RCPT TO:<jane.doe@acme.com>
250 OK                          ← the server would accept it
550 No such user                ← it would not
```

Enable it per-command with `--smtp`, or globally with `REACHOUT_SMTP=1`.

It fails, or misleads, more often than it works:

**Port 25 is usually blocked.** Nearly every residential ISP and cloud provider
blocks outbound port 25 to limit spam. You will get a timeout, reported as
`blocked` rather than as a result.

**Catch-all domains accept everything.** Many domains accept mail for any
address and sort it out later. ReachOut detects this by first probing a random
address that cannot exist; if the server accepts `zzqklmnprstuv@domain`, every
answer it gives is meaningless. The score is then capped at 72 and says so.

**Big providers never tell you.** Google Workspace and Microsoft 365 accept mail
for any address at the gateway and reject internally afterwards. ReachOut
recognises their MX records and skips the probe rather than reporting a
worthless `accepted`.

**It is visible.** RCPT probes appear in mail logs, and repeated probing from one
IP gets that IP blocked or reputation-flagged. That is why this is opt-in, one
address at a time, and never part of a batch by default.

## The honest summary

For a personal tool the achievable state of knowledge is:

> This address fits how this company demonstrably builds addresses, the domain
> can receive mail, and here is the public evidence behind that.

That is what a MEDIUM score means, and for reaching one person it is usually
enough. The only true confirmation is a reply.
