"""Runs before every scan. Each case is a real mistake found earlier; if any comes back, the scan
stops (and the dashboard keeps its last good data) instead of publishing wrong numbers.
Add a new line here whenever a new mistake is found."""
import math
import sys

from screener import core

VALUE_CASES = [  # (text, expected ₹ crore or None when no order value should be read)
    ("Nature of order components Order value ₹22 crores in aggregate", 22.0),                       # Kavveri
    ("A leading Indian bank with a total loan portfolio of ₹1,40,327 cr has selected Intellect", None),  # Intellect
    ("HCLTech has signed a multi-year deal with a leading US bank, a USD 14.7 billion company", None),     # HCL
    ('WABAG records all-time high Rs. 194 Bn order book', None),                                     # WABAG
    ("A bank with deposits of Rs 3,40,000 crore has chosen our core banking suite", None),
    ("The order book now stands at Rs 5,000 Cr after receiving a work order of Rs 320 Cr from BHEL", 320.0),
    ("Company has received an order worth Rs. 125.50 Crore from NHAI", 125.5),
    ("Received purchase order aggregating to Rs 4,50,00,000/- (inclusive of GST)", 4.5),
    ("Secured export order of USD 12 million from a European client", 105.6),
    ("Award of Order / Receipt of Order | Rs. 45.6 Cr from Central Railway for signalling works", 45.6),
    ("Magellanic Cloud Contract Worth &#8377;29.13 Crore", 29.13),
]
FILTER_CASES = [  # (exchange, headline, is it an order win?)
    ("NSE", "Action(s) taken or orders passed | Imposition of Fine for non compliance", False),        # Kavveri fine
    ("NSE", "General Updates | Tender Offer for USD Senior Secured Notes under Global Medium Term Note", False),  # Shriram
    ("NSE", "Updates | Cancellation of Letter of Award aggregating to ₹1,641.91 crore", False),        # Vikran
    ("NSE", "Press Release | Gross Order Value rose 48% YoY to INR 16,683 Cr", False),                 # Swiggy
    ("NSE", "Outcome of Board Meeting | non convertible debentures up to Rs. 25,000 crore", False),     # Reliance
    ("NSE", "Bagging/Receiving of orders/contracts | Bagging/Receiving of orders/contracts", True),
    ("NSE", "Press Release | BEL receives orders worth Rs. 1011 Crore", True),
    ("BSE", "Award of Order / Receipt of Order | Receipt of work order from NHAI", True),
]


def rupee_glyph_case():
    """'₹' drawn in a rupee font and read as '2': the reader must turn it back into ₹."""
    def ch(t, font): return {"text": t, "fontname": font}
    chars = [ch(c, "ABCDEF+Arial") for c in "Order value "] + [ch("2", "GHIJKL+RupeeFont")] + \
            [ch(c, "ABCDEF+Arial") for c in "22 crores"]
    core.repair_rupee_chars(chars)
    return "".join(c["text"] for c in chars)


def main():
    fails = []
    for text, exp in VALUE_CASES:
        v = core.extract_order_value(text)[0]
        if not ((exp is None and math.isnan(v)) or v == exp):
            fails.append(f"value  {text[:70]!r}: got {v}, expected {exp}")
    for ex, head, exp in FILTER_CASES:
        if core.is_order_win(ex, head, "") != exp:
            fails.append(f"filter {head[:70]!r}: expected {exp}")
    if rupee_glyph_case() != "Order value ₹22 crores":
        fails.append("rupee glyph repair failed: " + rupee_glyph_case())
    if fails:
        print("SELF-TEST FAILED — scan stopped so wrong numbers are not published:\n  " + "\n  ".join(fails))
        sys.exit(1)
    print(f"Self-test passed ({len(VALUE_CASES) + len(FILTER_CASES) + 1} checks)")


if __name__ == "__main__":
    main()
