#!/usr/bin/env python3
"""Dependency-free ISO 3166-1 alpha-2 country lookup for Job Scout."""

from __future__ import annotations

import re
import unicodedata


# Generated from Debian iso-codes' ISO 3166-1 short names. Keep this embedded so
# the helper behaves identically in the minimal OpenClaw gateway container.
_ISO_COUNTRIES = """
af|Afghanistan
al|Albania
dz|Algeria
as|American Samoa
ad|Andorra
ao|Angola
ai|Anguilla
aq|Antarctica
ag|Antigua and Barbuda
ar|Argentina
am|Armenia
aw|Aruba
au|Australia
at|Austria
az|Azerbaijan
bs|Bahamas
bh|Bahrain
bd|Bangladesh
bb|Barbados
by|Belarus
be|Belgium
bz|Belize
bj|Benin
bm|Bermuda
bt|Bhutan
bo|Bolivia, Plurinational State of
bq|Bonaire, Sint Eustatius and Saba
ba|Bosnia and Herzegovina
bw|Botswana
bv|Bouvet Island
br|Brazil
io|British Indian Ocean Territory
bn|Brunei Darussalam
bg|Bulgaria
bf|Burkina Faso
bi|Burundi
cv|Cabo Verde
kh|Cambodia
cm|Cameroon
ca|Canada
ky|Cayman Islands
cf|Central African Republic
td|Chad
cl|Chile
cn|China
cx|Christmas Island
cc|Cocos (Keeling) Islands
co|Colombia
km|Comoros
cg|Congo
cd|Congo, The Democratic Republic of the
ck|Cook Islands
cr|Costa Rica
hr|Croatia
cu|Cuba
cw|Curaçao
cy|Cyprus
cz|Czechia
ci|Côte d'Ivoire
dk|Denmark
dj|Djibouti
dm|Dominica
do|Dominican Republic
ec|Ecuador
eg|Egypt
sv|El Salvador
gq|Equatorial Guinea
er|Eritrea
ee|Estonia
sz|Eswatini
et|Ethiopia
fk|Falkland Islands (Malvinas)
fo|Faroe Islands
fj|Fiji
fi|Finland
fr|France
gf|French Guiana
pf|French Polynesia
tf|French Southern Territories
ga|Gabon
gm|Gambia
ge|Georgia
de|Germany
gh|Ghana
gi|Gibraltar
gr|Greece
gl|Greenland
gd|Grenada
gp|Guadeloupe
gu|Guam
gt|Guatemala
gg|Guernsey
gn|Guinea
gw|Guinea-Bissau
gy|Guyana
ht|Haiti
hm|Heard Island and McDonald Islands
va|Holy See (Vatican City State)
hn|Honduras
hk|Hong Kong
hu|Hungary
is|Iceland
in|India
id|Indonesia
ir|Iran, Islamic Republic of
iq|Iraq
ie|Ireland
im|Isle of Man
il|Israel
it|Italy
jm|Jamaica
jp|Japan
je|Jersey
jo|Jordan
kz|Kazakhstan
ke|Kenya
ki|Kiribati
kp|Korea, Democratic People's Republic of
kr|Korea, Republic of
kw|Kuwait
kg|Kyrgyzstan
la|Lao People's Democratic Republic
lv|Latvia
lb|Lebanon
ls|Lesotho
lr|Liberia
ly|Libya
li|Liechtenstein
lt|Lithuania
lu|Luxembourg
mo|Macao
mg|Madagascar
mw|Malawi
my|Malaysia
mv|Maldives
ml|Mali
mt|Malta
mh|Marshall Islands
mq|Martinique
mr|Mauritania
mu|Mauritius
yt|Mayotte
mx|Mexico
fm|Micronesia, Federated States of
md|Moldova, Republic of
mc|Monaco
mn|Mongolia
me|Montenegro
ms|Montserrat
ma|Morocco
mz|Mozambique
mm|Myanmar
na|Namibia
nr|Nauru
np|Nepal
nl|Netherlands
nc|New Caledonia
nz|New Zealand
ni|Nicaragua
ne|Niger
ng|Nigeria
nu|Niue
nf|Norfolk Island
mk|North Macedonia
mp|Northern Mariana Islands
no|Norway
om|Oman
pk|Pakistan
pw|Palau
ps|Palestine, State of
pa|Panama
pg|Papua New Guinea
py|Paraguay
pe|Peru
ph|Philippines
pn|Pitcairn
pl|Poland
pt|Portugal
pr|Puerto Rico
qa|Qatar
ro|Romania
ru|Russian Federation
rw|Rwanda
re|Réunion
bl|Saint Barthélemy
sh|Saint Helena, Ascension and Tristan da Cunha
kn|Saint Kitts and Nevis
lc|Saint Lucia
mf|Saint Martin (French part)
pm|Saint Pierre and Miquelon
vc|Saint Vincent and the Grenadines
ws|Samoa
sm|San Marino
st|Sao Tome and Principe
sa|Saudi Arabia
sn|Senegal
rs|Serbia
sc|Seychelles
sl|Sierra Leone
sg|Singapore
sx|Sint Maarten (Dutch part)
sk|Slovakia
si|Slovenia
sb|Solomon Islands
so|Somalia
za|South Africa
gs|South Georgia and the South Sandwich Islands
ss|South Sudan
es|Spain
lk|Sri Lanka
sd|Sudan
sr|Suriname
sj|Svalbard and Jan Mayen
se|Sweden
ch|Switzerland
sy|Syrian Arab Republic
tw|Taiwan, Province of China
tj|Tajikistan
tz|Tanzania, United Republic of
th|Thailand
tl|Timor-Leste
tg|Togo
tk|Tokelau
to|Tonga
tt|Trinidad and Tobago
tn|Tunisia
tr|Türkiye
tm|Turkmenistan
tc|Turks and Caicos Islands
tv|Tuvalu
ug|Uganda
ua|Ukraine
ae|United Arab Emirates
gb|United Kingdom
us|United States
um|United States Minor Outlying Islands
uy|Uruguay
uz|Uzbekistan
vu|Vanuatu
ve|Venezuela, Bolivarian Republic of
vn|Viet Nam
vg|Virgin Islands, British
vi|Virgin Islands, U.S.
wf|Wallis and Futuna
eh|Western Sahara
ye|Yemen
zm|Zambia
zw|Zimbabwe
ax|Åland Islands
"""


def normalize_country_name(value: object) -> str:
  text = unicodedata.normalize("NFKD", str(value or ""))
  text = "".join(char for char in text if not unicodedata.combining(char))
  return re.sub(r"[^a-z0-9]+", " ", text.casefold()).strip()


COUNTRY_CODE_BY_NAME: dict[str, str] = {}
COUNTRY_NAME_BY_CODE: dict[str, str] = {}
for _line in _ISO_COUNTRIES.strip().splitlines():
  _code, _name = _line.split("|", 1)
  COUNTRY_CODE_BY_NAME[normalize_country_name(_name)] = _code
  COUNTRY_NAME_BY_CODE[_code] = _name

_ALIASES = {
  "bolivia": "bo",
  "brunei": "bn",
  "cape verde": "cv",
  "congo brazzaville": "cg",
  "congo kinshasa": "cd",
  "democratic republic of congo": "cd",
  "drc": "cd",
  "ivory coast": "ci",
  "iran": "ir",
  "laos": "la",
  "moldova": "md",
  "north korea": "kp",
  "palestine": "ps",
  "russia": "ru",
  "south korea": "kr",
  "syria": "sy",
  "taiwan": "tw",
  "tanzania": "tz",
  "the bahamas": "bs",
  "the gambia": "gm",
  "turkey": "tr",
  "uae": "ae",
  "uk": "gb",
  "united states of america": "us",
  "usa": "us",
  "vatican": "va",
  "venezuela": "ve",
  "vietnam": "vn",
}
COUNTRY_CODE_BY_NAME.update({normalize_country_name(name): code for name, code in _ALIASES.items()})


def country_code(value: object) -> str | None:
  normalized = normalize_country_name(value)
  if len(normalized) == 2 and normalized in COUNTRY_NAME_BY_CODE:
    return normalized
  return COUNTRY_CODE_BY_NAME.get(normalized)


def country_name(code: str) -> str:
  return COUNTRY_NAME_BY_CODE[code]
