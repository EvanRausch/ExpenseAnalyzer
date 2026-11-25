"""
categories.py — the two-level category list: broad groups, each holding specific categories.

The list is the one described in docs/categories.md. Level 1 is the group ("Food & drink"),
level 2 the category ("Restaurants"). Every group also has a "<group> – Other" category.
Categories describe who was paid, not what was bought; a merchant that sells everything
(Amazon, Costco) has a category of its own.

People can add categories of their own; each one belongs to a group too (see
`custom` arguments below). Anything else, such as a category name from before this
list existed, is shown under the group "Other".
"""

from __future__ import annotations

OTHER_SUFFIX = " – Other"
# Where categories that are not in the list are shown
OTHER_GROUP = "Other"
# The group of lines that appear on statements but are not spending
NOT_EXPENSES = "Not expenses"

# (group, emoji, [(category, emoji), ...]) in display order
GROUPS: list[tuple[str, str, list[tuple[str, str]]]] = [
    (
        "Housing & home",
        "🏠",
        [
            ("Rent", "🏠"),
            ("Mortgage", "🏦"),
            ("Condo & association fees", "🏢"),
            ("Home services", "🔧"),
            ("Hardware & home improvement stores", "🛠️"),
            ("Furniture, décor & appliance stores", "🛋️"),
            ("Moving & storage", "📦"),
        ],
    ),
    (
        "Utilities & telecom",
        "💡",
        [
            ("Electricity", "⚡"),
            ("Gas & heating fuel", "🔥"),
            ("Water & waste", "🚰"),
            ("Phone, internet & TV", "📶"),
        ],
    ),
    (
        "Food & drink",
        "🍽️",
        [
            ("Supermarkets & grocery stores", "🛒"),
            ("Specialty food shops", "🥖"),
            ("Convenience stores", "🏪"),
            ("Meal kits & grocery delivery", "🥡"),
            ("Restaurants", "🍴"),
            ("Fast food & quick service", "🍔"),
            ("Coffee shops & desserts", "☕"),
            ("Bars, pubs & nightclubs", "🍺"),
            ("Food delivery apps", "🛵"),
            ("Cafeterias & vending machines", "🥪"),
        ],
    ),
    (
        "Alcohol, tobacco & cannabis",
        "🍷",
        [
            ("Liquor, wine & beer stores", "🍷"),
            ("Cannabis stores", "🌿"),
            ("Tobacco & vape shops", "🚬"),
        ],
    ),
    (
        "Transportation",
        "🚗",
        [
            ("Fuel & gas stations", "⛽"),
            ("Electric vehicle charging", "🔌"),
            ("Parking", "🅿️"),
            ("Tolls", "🛣️"),
            ("Vehicle purchase, lease & financing", "🚘"),
            ("Vehicle maintenance & repair", "🔩"),
            ("Vehicle registration & driver's licence", "🪪"),
            ("Public transit", "🚌"),
            ("Taxis & ride-hailing", "🚕"),
            ("Car, bike & scooter sharing", "🚲"),
        ],
    ),
    (
        "Travel",
        "✈️",
        [
            ("Flights", "✈️"),
            ("Hotels & lodging", "🏨"),
            ("Intercity train, bus & ferry", "🚆"),
            ("Car rental", "🚙"),
            ("Booking sites & travel agencies", "🧳"),
            ("Cruises, tours & packages", "🚢"),
            ("Airport services & duty-free", "🛫"),
        ],
    ),
    (
        "Shopping",
        "🛍️",
        [
            ("Online marketplaces", "📦"),
            ("Big-box, department & warehouse stores", "🏬"),
            ("Retail memberships", "🎫"),
            ("Discount & dollar stores", "🪙"),
            ("Clothing, shoes & accessories", "👕"),
            ("Electronics & computers", "💻"),
            ("Books, stationery & office supplies", "📚"),
            ("Second-hand & thrift stores", "♻️"),
        ],
    ),
    (
        "Health & personal care",
        "🩺",
        [
            ("Pharmacies & drugstores", "💊"),
            ("Doctors, clinics & hospitals", "🏥"),
            ("Dental care", "🦷"),
            ("Vision care & eyewear", "👓"),
            ("Therapy & paramedical care", "💆"),
            ("Medical labs & imaging", "🧪"),
            ("Medical equipment & supplies", "🩹"),
            ("Health food & supplement stores", "🌱"),
            ("Elder & home care", "🧓"),
            ("Hair salons & barbers", "💈"),
            ("Spa & beauty services", "💅"),
            ("Cosmetics & beauty stores", "💄"),
            ("Laundry, dry cleaning & alterations", "🧺"),
        ],
    ),
    (
        "Entertainment & digital subscriptions",
        "🎬",
        [
            ("Video & music streaming", "📺"),
            ("Events & tickets", "🎟️"),
            ("Attractions & museums", "🎡"),
            ("Video games", "🎮"),
            ("App stores & platform billing", "📱"),
            ("News, books & audio subscriptions", "📰"),
            ("Software & cloud services", "☁️"),
            ("Creator, social & dating subscriptions", "💬"),
            ("Gambling & lottery", "🎰"),
        ],
    ),
    (
        "Sports, hobbies & recreation",
        "🏋️",
        [
            ("Gyms & fitness studios", "🏋️"),
            ("Sports facilities & passes", "⛷️"),
            ("Leagues, clubs & event registrations", "🏅"),
            ("Outdoor recreation & parks", "🏕️"),
            ("Sporting goods & outdoor gear", "🎿"),
            ("Hobby, craft, toy & music stores", "🎨"),
            ("Recreational classes & lessons", "🎻"),
        ],
    ),
    (
        "Education & professional",
        "🎓",
        [
            ("Tuition & school fees", "🎓"),
            ("Courses & training", "📖"),
            ("School books & supplies", "✏️"),
            ("Professional dues & licences", "📜"),
        ],
    ),
    (
        "Family & pets",
        "👨‍👩‍👧",
        [
            ("Childcare", "🧸"),
            ("Children's activities & camps", "⛺"),
            ("Baby & children's stores", "👶"),
            ("Child & spousal support", "🤝"),
            ("Veterinary care", "🐾"),
            ("Pet stores & supplies", "🐱"),
            ("Pet services", "🐕"),
        ],
    ),
    (
        "Insurance",
        "🛡️",
        [
            ("Home & vehicle insurance", "🛡️"),
            ("Life & disability insurance", "🧬"),
            ("Health, dental & travel insurance", "⚕️"),
            ("Pet insurance", "🐾"),
            ("Device protection & warranties", "📲"),
        ],
    ),
    (
        "Bank fees, interest & debt",
        "🏦",
        [
            ("Account & transaction fees", "🏦"),
            ("Interest charges", "📉"),
            ("Loan payments", "💳"),
            ("Buy-now-pay-later instalments", "🗓️"),
        ],
    ),
    (
        "Taxes & government",
        "🏛️",
        [
            ("Income tax payments", "🧾"),
            ("Property & school taxes", "🏘️"),
            ("Fines & tickets", "🚨"),
            ("Government fees & documents", "🏛️"),
        ],
    ),
    (
        "Gifts & donations",
        "🎁",
        [
            ("Charities & non-profits", "💛"),
            ("Religious organisations", "🕊️"),
            ("Political contributions", "🗳️"),
            ("Crowdfunding", "🫶"),
            ("Florists, gift shops & gift cards", "🎁"),
        ],
    ),
    (
        "Professional & other services",
        "💼",
        [
            ("Legal & notary services", "⚖️"),
            ("Accounting & tax preparation", "🧮"),
            ("Postal, shipping & printing", "📮"),
            ("Event & ceremony services", "💐"),
            ("Business services", "💼"),
        ],
    ),
    (
        "Cash, transfers & unidentified",
        "💸",
        [
            ("Cash withdrawals", "🏧"),
            ("Transfers to people", "💸"),
            ("International money transfers", "🌍"),
            ("Cheques & bank drafts", "🖋️"),
            ("Payment processors with hidden merchant", "🔒"),
            ("Bill payments to unrecognised payees", "📋"),
            ("Unrecognised merchants", "❓"),
        ],
    ),
    (
        NOT_EXPENSES,
        "💰",
        [
            ("Salary & wages", "💰"),
            ("Government benefits & tax refunds", "🏛️"),
            ("Investment & interest income", "📈"),
            ("Other income", "💵"),
            ("Transfers received from people", "🤲"),
            ("Insurance claims & reimbursements", "↩️"),
            ("Cashback & rewards", "🎉"),
            ("Transfers between own accounts", "🔁"),
            ("Credit-card payments", "💳"),
            ("Savings & investment contributions", "🏦"),
        ],
    ),
]

GROUP_NAMES: list[str] = [name for name, _, _ in GROUPS]
GROUP_EMOJI: dict[str, str] = {name: emoji for name, emoji, _ in GROUPS}
GROUP_EMOJI[OTHER_GROUP] = "🏷️"


def _other(group: str) -> str:
    return group + OTHER_SUFFIX


# category -> group, for every category in the list including each group's "Other"
GROUP_OF: dict[str, str] = {}
# category -> emoji, likewise
CATEGORY_EMOJI: dict[str, str] = {}
for _group, _emoji, _categories in GROUPS:
    for _category, _category_emoji in _categories:
        GROUP_OF[_category] = _group
        CATEGORY_EMOJI[_category] = _category_emoji
    GROUP_OF[_other(_group)] = _group
    CATEGORY_EMOJI[_other(_group)] = _emoji
CATEGORY_EMOJI[OTHER_GROUP] = "🏷️"

# Every category in the list, in display order
ALL_CATEGORIES: list[str] = list(GROUP_OF)

# Categories from before this list, where the new one is clear. None: let Gemini decide.
LEGACY: dict[str, str | None] = {
    "Amazon Purchase": "Online marketplaces",
    "Apple Services": "App stores & platform billing",
    "Streaming": "Video & music streaming",
    "Software/SaaS": "Software & cloud services",
    "Rent": "Rent",
    "Ride-sharing": "Taxis & ride-hailing",
    "Food Delivery": "Food delivery apps",
    "Groceries": "Supermarkets & grocery stores",
    "Food & Dining": None,
    "Pharmacy": "Pharmacies & drugstores",
    "Health & Medical": None,
    "Dentist/Medical": "Dental care",
    "Clothing & Fashion": "Clothing, shoes & accessories",
    "Electronics": "Electronics & computers",
    "Entertainment": None,
    "Transport": None,
    "Automotive": None,
    "Travel & Accommodation": None,
    "Utilities": None,
    "Hydro": "Electricity",
    "Telecom & Internet": "Phone, internet & TV",
    "Payroll / Income": "Salary & wages",
    "Interac Transfer": "Transfers to people",
    "Bill Payment": None,
    "Investment": "Savings & investment contributions",
    "Insurance": None,
    "Bank Fee": "Account & transaction fees",
    "ATM": "Cash withdrawals",
    "Government": None,
    "Education": None,
    "Charity": "Charities & non-profits",
    "Sports & Fitness": None,
    "Golf": "Sports facilities & passes",
    "Ski": "Sports facilities & passes",
    "Pets": None,
    "Cat": "Pet stores & supplies",
    "Personal Care": None,
    "Bars/Festivals/Alcohol": None,
    "Weed": "Cannabis stores",
    "e-Commerce / Online Shopping": None,
    "Other": None,
}


def group_of(category: str, custom: dict[str, str] | None = None) -> str:
    """The group a category belongs to; "Other" for a name not in the list."""
    category = (category or "").strip()
    if category in GROUP_OF:
        return GROUP_OF[category]
    group = (custom or {}).get(category, "")
    return group if group in GROUP_EMOJI else OTHER_GROUP


def emoji_of(category: str) -> str | None:
    """The list's emoji for a category or a group, or None for a name not in it."""
    return CATEGORY_EMOJI.get(category) or GROUP_EMOJI.get(category)


def known_categories(custom: dict[str, str] | None = None) -> list[str]:
    """The list's categories followed by the person's own, in display order."""
    return ALL_CATEGORIES + [c for c in (custom or {}) if c not in GROUP_OF]


def grouped(
    custom: dict[str, str] | None = None, extra: list[str] = ()
) -> list[tuple[str, list[str]]]:
    """[(group, [categories])] in display order, with the person's own categories and
    any `extra` names (such as those found in the data) placed in their groups."""
    members: dict[str, list[str]] = {name: [] for name in GROUP_NAMES}
    members[OTHER_GROUP] = []
    for category in known_categories(custom):
        members[group_of(category, custom)].append(category)
    for category in extra:
        group = group_of(category, custom)
        if category not in members[group]:
            members[group].append(category)
    return [(group, names) for group, names in members.items() if names]


def normalise(answer: str, custom: dict[str, str] | None = None) -> str:
    """Turn Gemini's answer into a category of the list ("Other" if it is nothing like one)."""
    text = (answer or "").strip()
    if not text:
        return OTHER_GROUP
    known = known_categories(custom)
    if text in known:
        return text
    lowered = {c.lower(): c for c in known}
    simplified = text.replace("-", "–").lower()
    if simplified in lowered:
        return lowered[simplified]
    # "Food & drink" or "Food & drink – Something" -> that group's Other
    head = simplified.split("–")[0].strip()
    for group in GROUP_NAMES:
        if head == group.lower():
            return _other(group)
    return LEGACY.get(text) or OTHER_GROUP
