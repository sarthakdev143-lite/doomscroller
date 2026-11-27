# Legal pages: open items

The public legal pages (privacy, terms, refunds, cookies; `saas/web/templates/legal/`) no longer show draft
markers. Every marker that was visible is listed here, with what the page says in its place, so the owner can
take this list to a lawyer. In the templates each one is now a Jinja comment (`{# LAWYER REVIEW: ... #}` or
`{# OWNER: ... #}`) at the same spot, so it is invisible on the site but still beside the sentence it concerns.

The operator is named from settings (`saas/config.py`, web section), so fix these by environment, not by
editing the pages:

| setting | default | used for |
|---------|---------|----------|
| `SAAS_OPERATOR_NAME` | "the operator of <brand>", so "the operator of doomscroller" | controller (privacy 1), the agreement party (terms 1) |
| `SAAS_OPERATOR_COUNTRY` | India | the same lines, transfers (privacy 7), law and disputes (terms 12) |
| `SAAS_LEGAL_EFFECTIVE_DATE` | the export date, e.g. 27 November 2026 | "Last updated" and "Effective" on every page |
| `SAAS_SUPPORT_EMAIL` | none: the export fails without it unless `--allow-placeholder` | every contact line |

Still needed before launch, besides the rows below: a real support address, the operator's legal name and postal
address, and a lawyer's review of all four pages for each country we sell in.

| page and section | marker that was shown | what the page says now |
|------------------|-----------------------|------------------------|
| All legal pages (header) | [LAWYER REVIEW: date] for Last updated and Effective | Shows SAAS_LEGAL_EFFECTIVE_DATE, else the export date ("27 November 2026"). |
| All legal pages (header) | Visible banner: "DRAFT. Text marked [LAWYER REVIEW] must be written or approved by a qualified lawyer for every country we sell in before launch." | Banner removed. The requirement stands: this whole list. |
| Privacy, 1. Who we are | [LAWYER REVIEW: legal entity or sole trader name, address] | "...is {operator_name}, an individual based in {operator_country}." Set SAAS_OPERATOR_NAME / SAAS_OPERATOR_COUNTRY. No postal address is shown yet. |
| Privacy, 1. Who we are | [LAWYER REVIEW: data protection officer or EU/UK representative, if required] | Nothing shown. |
| Privacy, 2. What we collect (website) | [confirm what the waitlist endpoint stores] | Replaced with what the edge waitlist actually stores (edge/migrations: ip_hash, net_hash, country, region, city, timezone, asn, as_org, colo, user_agent). Re-check if that table changes. |
| Privacy, 3. Why we use it | Legal basis "Consent" for the waitlist, marked [LAWYER REVIEW] | "Consent" as plain text. |
| Privacy, 3. Why we use it | Legitimate interests for anti-fraud: [LAWYER REVIEW: balancing test] | "Legitimate interests" as plain text. |
| Privacy, 3. Why we use it | Legal basis "Contract" for the service, marked [LAWYER REVIEW] | "Contract" as plain text. |
| Privacy, 3. Why we use it | Legal basis "Legal obligation" for tax records, marked [LAWYER REVIEW] | "Legal obligation" as plain text. |
| Privacy, 4. Analytics and recordings | [LAWYER REVIEW: consent wording per region; Google and Microsoft as processors or independent controllers] | Paragraph removed. |
| Privacy, 5. Bots and abuse | [LAWYER REVIEW: whether device signals need consent in each market] | Nothing shown. |
| Privacy, 6. Who processes it | [payment provider, named at launch] | "Our payment provider" (the site does not name it yet). |
| Privacy, 6. Who processes it | AI model providers [confirm list] | "AI model providers", unnamed. |
| Privacy, 6. Who processes it | [email provider] | "Our email provider", unnamed. |
| Privacy, 6. Who processes it | Visible note: [LAWYER REVIEW: confirm every provider, data processing terms, and each provider’s terms on training with customer data.] | Note removed. |
| Privacy, 7. International transfers | [LAWYER REVIEW: where data is stored, which providers are outside your country, and the transfer safeguards used.] | A factual sentence: run from {operator_country}; several providers are elsewhere, mostly the US. No safeguards are claimed. |
| Privacy, 8. How long we keep it | Waitlist entries: at most [LAWYER REVIEW: period] | No maximum period is stated. |
| Privacy, 8. How long we keep it | Google Analytics: [2 or 14 months, as set]. Clarity: [as set by Microsoft] | "Google Analytics: at most 14 months. Clarity: the period Microsoft sets for Clarity." |
| Privacy, 8. How long we keep it | Account data: while open, then [LAWYER REVIEW: period] | Describes what delete_account (saas/auth/service.py) actually does. |
| Privacy, 8. How long we keep it | Payment records: [LAWYER REVIEW: period per country] | "As long as tax law requires". |
| Privacy, 9. Your rights | [LAWYER REVIEW: GDPR / UK GDPR, India DPDP Act 2023, California CCPA/CPRA wording and response times] | Paragraph removed; the general rights paragraph above it stays. |
| Privacy, 10. Children | [LAWYER REVIEW: minimum age per market] | "The service is not for children." |
| Privacy, 11. Security | [LAWYER REVIEW: breach notification commitments] | Nothing shown. |
| Cookies, 2. On this website | Clarity's Microsoft cookies (MUID, CLID, ANONCHK, SM, MR) [confirm] | The list stays as written, unmarked. Check it against the live site once Clarity is on. |
| Cookies, 2. On this website | cf_clearance [confirm], set by Cloudflare Turnstile, lasts [confirm] | "cf_clearance, only if Cloudflare shows you a challenge", lasting "as long as Cloudflare's challenge setting allows". |
| Cookies, 2. On this website | (missing row) ds_landing session storage was not listed | Added: the first page and referrer, kept for the tab and sent with the waitlist form (privacy 2 already says so). |
| Cookies, app cookies table | csrf cookie lifetime [confirm] | "1 year" (Max-Age=31536000 in saas/security/middleware.py). |
| Cookies, 4. Your choices | [LAWYER REVIEW: consent wording per region] | Nothing shown. |
| Refunds, 1. The short version | [owner + LAWYER REVIEW: number] days | 14 days, from the owner's pricing memo (docs/saas/costs-and-pricing.md, 5.8). Owner to confirm. |
| Refunds, 3. Pay as you go | [number] days | 14 days, as above. |
| Refunds, 3. Pay as you go | [owner decision: partial refunds of partly used purchases?] | Nothing shown; the page says a partly used purchase is not refundable. |
| Refunds, 4. Monthly | [owner + LAWYER REVIEW: refunds for a renewal you did not mean to keep; how cancelling works with the payment provider] | Nothing shown. (The pricing memo suggests: first monthly charge refundable within 7 days if 60 minutes or fewer were used.) |
| Refunds, 5. Clips you are unhappy with | [owner decision] | Nothing shown; the page says each case is looked at and minutes may be returned. |
| Refunds, 7. How to ask | usually within [number] business days | "...how long they take to arrive depends on the payment provider and your bank." |
| Refunds, 7. How to ask | [LAWYER REVIEW: statutory rights, e.g. the EU/UK 14-day right of withdrawal for digital content and how it ends once a job starts with your consent] | "This policy does not take away any refund rights the law gives you where you live." |
| Terms, 1. The agreement | [LAWYER REVIEW: legal entity] | "...between you and {operator_name}, an individual based in {operator_country} (\"we\")". |
| Terms, 2. Accounts | You must be at least [LAWYER REVIEW: age] | "You must be old enough, where you live, to enter a binding contract." No number. |
| Terms, 3. Your content | [LAWYER REVIEW: licence wording] | The plain-language licence sentence stays. |
| Terms, 5. Minutes and payment | [LAWYER REVIEW: name it] (payment provider); [LAWYER REVIEW: expiry of unused minutes, price-change notice] | "...through our payment provider." Nothing about expiry or price-change notice. |
| Terms, 5. Minutes and payment | Prices [include / exclude] taxes. [LAWYER REVIEW: VAT/GST handling per country] | "The price you pay is the one shown at checkout." |
| Terms, 8. API and MCP | [notice period] notice | "with reasonable notice". |
| Terms, 9. Copyright complaints | [LAWYER REVIEW: copyright agent address and notice procedure (e.g. DMCA, IT Rules 2021)] | Points to the support address. No designated agent or grievance officer is named yet. |
| Terms, 10. Suspension and closing | [LAWYER REVIEW: what happens to unused paid minutes] | Nothing shown. |
| Terms, 11. Warranties and liability | [LAWYER REVIEW: warranty disclaimer, limitation of liability, indemnity, consumer-law carve-outs] | Two plain sentences; no disclaimer or liability cap is claimed. |
| Terms, 12. Law and disputes | [LAWYER REVIEW: governing law, courts or arbitration, consumer rights in the customer’s country] | Where it is run from, write to us first, consumer rights kept. No governing law or forum is chosen. |
