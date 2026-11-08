"""The shared cases for what a card or account number is (sync.card_number).

Every number here is invented: test-card shapes ("4000 0000 0000 0002",
"5500 0000 0000 0004", "3400 000000 00009") or plainly made-up digits,
never a real card. Each case is (text, what `cut` makes of it). A case
whose cut equals its text holds no number.

Consumed by the module's own test, the SQL-vs-Python parity test and the
tests of every door that cuts a name with it, so a change to the grammar
shows up everywhere at once.
"""

# (text, cut)
CASES = [
    # the two card layouts, any of the separators, mixed or doubled
    ("Visa 4000 1234 5678 9010", "Visa …9010"),
    ("Visa 4000-1234-5678-9010", "Visa …9010"),
    ("Visa 4000.1234.5678.9010", "Visa …9010"),
    ("Visa 4000/1234/5678/9010", "Visa …9010"),
    ("Visa 4000  1234  5678  9010", "Visa …9010"),
    ("Visa 4000-1234 5678-9010", "Visa …9010"),
    ("Visa 4000 - 1234 - 5678 - 9010", "Visa …9010"),
    ("Card 1234 / 5678 / 9012 / 3456", "Card …3456"),
    # any whitespace is a space: a spreadsheet cell's tab, a statement's
    # no-break space, a line break, and they join as a space does
    ("Visa 4000\t1234\t5678\t9010", "Visa …9010"),
    ("Visa 4000\u00a01234\u00a05678\u00a09010", "Visa …9010"),
    ("Visa 4000 1234\t5678\r\n9010", "Visa …9010"),
    ("2024\t4000 0000\t0000 0002", "…0002"),
    ("Amex 3400\t000000\t00009", "Amex …0009"),
    ("4000.0000 0000 0000 0002", "…0002"),
    ("Amex 3400 000000 00009", "Amex …0009"),
    ("Amex 3400-000000-00009", "Amex …0009"),
    ("Amex 3400.000000.00009", "Amex …0009"),
    # unbroken runs of eight or more
    ("Visa 4000000000000002", "Visa …0002"),
    ("Checking 12345678", "Checking …5678"),
    ("Savings 123456789012", "Savings …9012"),
    # further groups joined as the fourth was are cut with the number
    ("2024 4000 0000 0000 0002", "…0002"),
    ("4000-0000-0000-0002-1234", "…1234"),
    ("Visa 4000 0000 0000 0002 2025", "Visa …2025"),
    # what follows a number is not pulled into it
    ("Visa 4000 0000 0000 0002 - 2025", "Visa …0002 - 2025"),
    ("Chase 4000 0000 0000 0002, 05/27", "Chase …0002, 05/27"),
    ("Visa 4000 0000 0000 0002 (2)", "Visa …0002 (2)"),
    ("Card 4000-0000-0000-0002 #2", "Card …0002 #2"),
    ("Visa 4000 0000 0000 0002 123", "Visa …0002 123"),
    # a door's "-N" collision suffix stays beside the cut last four
    ("Visa 4000 0000 0000 0002-2", "Visa …0002-2"),
    ("visa-4000-0000-0000-0002-2", "visa-…0002-2"),
    # what stands before a number stays too
    ("Card 12 4000 0000 0000 0002", "Card 12 …0002"),
    ("Two 4000 0000 0000 0002 / 5500 0000 0000 0004", "Two …0002 / …0004"),
    # a number that only shows once its unbroken run is cut: the fixed point
    ("Card 12345678 1234 1234 1234", "Card …1234"),
    # what is already cut stays cut, and is never marked twice
    ("Visa …0002", "Visa …0002"),
    ("Visa …1234 5678 9012 3456", "Visa …3456"),
    # not numbers: a trailing short group, years, dates, statement periods,
    # phone numbers, seven-digit groups
    ("Checking 0012 3456 7890 123", "Checking 0012 3456 7890 123"),
    ("FY 2023-2024", "FY 2023-2024"),
    ("College 2023 - 2024", "College 2023 - 2024"),
    ("CD 03/15/2025 09/15/2025", "CD 03/15/2025 09/15/2025"),
    ("CD 3/15/2025-9/15/2025", "CD 3/15/2025-9/15/2025"),
    ("T-Bill 2025-03-15 2025-09-15", "T-Bill 2025-03-15 2025-09-15"),
    ("Statement 2021-01-01_2021-12-31", "Statement 2021-01-01_2021-12-31"),
    ("Stmt 01.01.2024 - 31.12.2024", "Stmt 01.01.2024 - 31.12.2024"),
    ("Call 555-201-7788", "Call 555-201-7788"),
    ("Support +1 800 555 7788", "Support +1 800 555 7788"),
    ("Phone 555.201.7788", "Phone 555.201.7788"),
    ("Ref 1234567 7654321", "Ref 1234567 7654321"),
    ("FY\t2023\t2024", "FY\t2023\t2024"),
    ("CD\t03/15/2025\t09/15/2025", "CD\t03/15/2025\t09/15/2025"),
    ("Order 123-4567890-1234567", "Order 123-4567890-1234567"),
    # two year ranges side by side are four groups of four: read as one
    # number, as the account id's slug has always read them
    ("Tuition 2023-2024 2024-2025", "Tuition …2025"),
    # a pathological cell: cut whole, in linear time
    ("1234 " * 2000, "…1234 "),
]

# The cases a separator other than a space, dash, dot or slash joins: the
# frozen cuts of 203, 204 and 206 read none of them (206's is the module's
# grammar as it stood before whitespace was folded into it)
TABBED = {
    "Visa 4000\t1234\t5678\t9010",
    "Visa 4000\u00a01234\u00a05678\u00a09010",
    "Visa 4000 1234\t5678\r\n9010",
    "2024\t4000 0000\t0000 0002",
    "Amex 3400\t000000\t00009",
}

# Where the frozen cut of migrations 203 and 204 (one repeated space or
# dash, one pass) gives another answer than the module. Every other case
# the migrations cut, they cut exactly as the module does. These are what a
# later migration has to re-cut in stored text.
FROZEN_DIFFERS = {
    "Visa 4000.1234.5678.9010",
    "Visa 4000/1234/5678/9010",
    "Visa 4000  1234  5678  9010",
    "Visa 4000-1234 5678-9010",
    "Visa 4000 - 1234 - 5678 - 9010",
    "Card 1234 / 5678 / 9012 / 3456",
    "4000.0000 0000 0000 0002",
    "Amex 3400.000000.00009",
    "Card 12345678 1234 1234 1234",
    "Visa …1234 5678 9012 3456",
    "Tuition 2023-2024 2024-2025",
    *(t for t in TABBED),
}

# Of those, the stored text the frozen cut left that the module does not
# cut on to its own answer: the frozen cut took a number's last four
# groups and left its first in front of the mark ("4000.…0002"), or
# marked a cut number twice ("……3456"). Neither holds more than four
# digits beside a last four, which is no number, so they stay as stored.
FROZEN_LEAVES = {"4000.0000 0000 0000 0002", "Visa …1234 5678 9012 3456"}
