# Borehole knowledge

`borehole-providers.json` is private operator data. Only the dedicated lookup
reads it; never append the raw catalog to `business_info` or a customer prompt.
Keep provider names, bases, source captions and provenance internal.

Update offers here when a provider confirms changes. Prices are USD, with a
price of `null` meaning unknown, never free. Do not copy one provider's rates,
specifications, payment terms or coverage into another provider's record.
Offers are separated by service, requested pump HP and casing class. A drilling
subtotal covers the first depth block plus extra meters and excludes other work.
Skylight Solar is outside this catalog and retains its independent fixed
$10/month advertising arrangement.

Locations were confirmed by the operator on 2026-11-01. Same Age Projects' rates
come from the pre-migration business information. Retired campaign source
captions are preserved in the other records for provenance. Their cron removal
does not retire these providers. Changes require the normal source deployment.

`zimbabwe-towns.json` is a compact extract of populated places from GeoNames'
Zimbabwe dump, retrieved 2026-11-01. It includes towns with population at least
1000 and additional named coverage towns. Source:
https://download.geonames.org/export/dump/ZW.zip

GeoNames data is licensed under CC BY 4.0:
https://creativecommons.org/licenses/by/4.0/
Attribution: GeoNames, https://www.geonames.org/.
This extract is adapted to retain names, aliases, coordinates and GeoNames IDs.
Distances are straight-line town-centre comparisons, not road or travel quotes.
Unknown or ambiguous locations require a nearest recognized town. Do not infer
a customer's location from the advert group or phone number.
