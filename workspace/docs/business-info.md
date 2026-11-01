# Genaie Business Info

This page is what the public WhatsApp bot answers from, via the
`public_service` `business_info` action. Edit prices and details here; the bot
reads this file on every lookup, so a deploy is all a change needs. If something
is not written here, the bot does not know it and must hand the customer off
instead of guessing.

## How we work (all businesses)

- All our services operate all around Zimbabwe.
- If asked where we are, where we are based, where we are located, where we
  are from, or whether we serve their area, say: "All our services operate all around Zimbabwe. Where are you from?"
- Use the same response for more specific location questions (e.g.
  "Munowanikwa muBindura papi?"), in the customer's language. Do not describe
  us as based in Bindura or give a suburb, street, address, office or yard.
- Genaie advertises three partner businesses (fruit trees, borehole drilling,
  building materials) and also offers its own services (CVs, marketing).

## Building materials

Prices:
- Bricks, red commons: $95 per 1000
- Bricks, blue heart: $100 per 1000
- Quarry stones: $25 per cubic
- River sand: $350 per 15 cubic
- Pit sand: $340 per 15 cubic
- Gravel: $220 per 15 cubic
- Bulk quantities available.
- Transport within local Bindura: $550.

To take an order, find out: which material (for bricks: red commons or blue
heart), how much, and where to deliver.

Not known, so hand off: transport anywhere other than local Bindura, delivery
dates or times, stock levels, discounts, anything else not listed above.

## Fruit trees

Grafted mango, pawpaw, orange, naartjie and lemon trees.

Prices:
- Small (nursery size): $5 per tree
- Medium: $15 per tree
- Big: $20 per tree
- Wholesale, small (nursery size): $3 per tree, minimum 20 trees
- Wholesale, medium or big: available from 100 trees
- Below these quantities the retail prices apply.

To take an order, find out: which trees, what size, how many, and where to
deliver.

Not known, so hand off: transport or delivery price, the wholesale price for
medium or big trees, delivery dates, anything else not listed above.

## Borehole drilling

For every borehole enquiry, use `public_service` action `borehole_info`.
Ask for the customer's town or nearest major town and the service they need.
The lookup matches documented offers by location and requirements; use only
its returned prices, packages, inclusions and conditions. Re-query when the
customer changes location or requirements, and for follow-up borehole questions.
Never reuse the old generic drilling rates or assume all offers come from the
same supplier. Missing prices, travel charges, timing or site-specific details
require a borehole handoff. Continue all enquiries through Genaie.
Company identities and base locations are private and must not be disclosed.

## CVs

- Professional CV writing and CV revamping: $5, paid by EcoCash.
- The customer can pick a template from the catalogue, or send one they like,
  plus the details they want on the CV. They pay, then the CV is made.
- An automated AI interview can collect the details for the CV right here in
  the chat.

## Business services (Genaie)

- Lead generation: we find potential customers for a business. The business
  chooses how to pay: per lead, a fixed monthly payment, or a % of revenue.
- WhatsApp group adverts.

Prices for these are quoted per customer, so hand off once you know what they
need.

## Jobs

- We run a WhatsApp group that posts job vacancies. Job seekers can be added if
  they want.
- We do not search for jobs or send applications for anyone. We can make or
  revamp their CV.

## Website

genaie.site
