"""SYNTHETIC TEST FIXTURES - fictional content shaped like RBI pages.

These documents are NOT real RBI circulars and their numbers/rules are invented.
They exist only to test parsing, relation extraction and temporal logic offline.
"""
from __future__ import annotations

import json
from pathlib import Path

PAGE = """<html><head><title>Reserve Bank of India - Notifications</title></head><body>
<table class="tablebg"><tr><td class="tableheader"><b>{title}</b></td></tr>
<tr><td class="tablecontent2">
<p>{ref_no}<br/>{dept_ref}</p>
<p>{date_str}</p>
<p>{addressee}</p>
<p>Madam / Dear Sir,</p>
<p><b>{title}</b></p>
{body}
<p>Yours faithfully,</p><p>(Fictional Officer)<br/>Chief General Manager</p>
</td></tr></table></body></html>"""

DOCS = [
    dict(num="9001", title="Master Circular - Loans against Gold Ornaments (SYNTHETIC)",
         ref_no="RBI/2019-20/11", dept_ref="DOR.CRE.No.BC.11/21.04.048/2019-20", date_str="July 1, 2019",
         addressee="All Scheduled Commercial Banks",
         body="""<p>1. This Master Circular consolidates instructions on loans against gold ornaments.</p>
<p>2. The loan to value (LTV) ratio for loans against gold ornaments shall not exceed 75 per cent of the value of gold.</p>
<p>3. Banks shall value gold at the average closing price of 22 carat gold for the preceding 30 days.</p>
<p>4. Bullet repayment loans against gold shall not exceed Rs 2 lakh per borrower.</p>"""),
    dict(num="9002", title="Enhancement of LTV for Loans against Gold Ornaments (SYNTHETIC)",
         ref_no="RBI/2020-21/31", dept_ref="DOR.CRE.No.BC.31/21.04.048/2020-21", date_str="August 6, 2020",
         addressee="All Scheduled Commercial Banks",
         body="""<p>1. Please refer to Master Circular DOR.CRE.No.BC.11/21.04.048/2019-20 dated July 1, 2019 on loans against gold ornaments.</p>
<p>2. In partial modification of the said Master Circular, it has been decided to increase the permissible loan to value (LTV) ratio for loans against gold ornaments from 75 per cent to 90 per cent.</p>
<p>3. These instructions shall come into force with effect from August 6, 2020.</p>"""),
    dict(num="9003", title="Master Direction - Reserve Bank of India (Gold Loans) Directions, 2022 (SYNTHETIC)",
         ref_no="RBI/2022-23/05", dept_ref="DOR.CRE.REC.No.05/21.04.048/2022-23", date_str="April 1, 2022",
         addressee="All Scheduled Commercial Banks",
         body="""<p>1. These Directions shall be called the Reserve Bank of India (Gold Loans) Directions, 2022.</p>
<p>2. These Directions shall come into force with effect from May 1, 2022.</p>
<p>3. The loan to value (LTV) ratio for loans against gold ornaments shall not exceed 75 per cent.</p>
<p>4. Bullet repayment loans against gold shall not exceed Rs 4 lakh per borrower.</p>
<p>5. Banks shall have a board-approved policy on auction of pledged gold, with at least two public notices before auction.</p>
<p>6. Banks shall maintain records of all gold valuation certificates for at least five years.</p>
<p>7. Repeal: With the issue of these Directions, the instructions contained in the circulars listed below stand repealed.</p>
<p>Annex - List of circulars repealed</p>
<p>1. DOR.CRE.No.BC.11/21.04.048/2019-20 dated July 1, 2019</p>
<p>2. DOR.CRE.No.BC.31/21.04.048/2020-21 dated August 6, 2020</p>"""),
    dict(num="9004", title="Periodic Updation of KYC (SYNTHETIC)",
         ref_no="RBI/2015-16/200", dept_ref="DBR.AML.No.BC.20/14.01.001/2015-16", date_str="February 25, 2016",
         addressee="All Regulated Entities",
         body="""<p>1. Regulated entities shall carry out periodic updation of KYC at least once every two years for high risk customers, once every eight years for medium risk customers and once every ten years for low risk customers.</p>
<p>2. Periodic updation shall require a fresh certified copy of an officially valid document.</p>
<p>3. These instructions shall come into effect immediately.</p>"""),
    dict(num="9005", title="Master Direction - Know Your Customer (KYC) Directions, 2023 (SYNTHETIC)",
         ref_no="RBI/2022-23/180", dept_ref="DOR.AML.REC.No.18/14.01.001/2022-23", date_str="January 10, 2023",
         addressee="All Regulated Entities",
         body="""<p>1. These Directions shall be called the Know Your Customer (KYC) Directions, 2023 and shall come into effect immediately.</p>
<p>2. Regulated entities shall carry out periodic updation of KYC at least once every two years for high risk customers and once every ten years for medium and low risk customers.</p>
<p>3. In case of no change in KYC information, a self-declaration from the customer through registered email or mobile number shall be sufficient.</p>
<p>4. In supersession of the instructions contained in circular DBR.AML.No.BC.20/14.01.001/2015-16 dated February 25, 2016, these Directions shall apply.</p>"""),
    dict(num="9006", title="Tokenisation of Card Transactions (SYNTHETIC)",
         ref_no="RBI/2020-21/150", dept_ref="DPSS.CO.PD.No.150/02.14.003/2020-21", date_str="March 17, 2021",
         addressee="All Card Networks and Card Issuers",
         body="""<p>1. Card issuers may offer card tokenisation services to cardholders with explicit consent.</p>
<p>2. The customer due diligence norms in circular DBR.AML.No.BC.20/14.01.001/2015-16 dated February 25, 2016 continue to apply to cardholders.</p>
<p>3. No charges shall be recovered from the customer for availing tokenisation.</p>"""),
    dict(num="9007", title="Amendment to the KYC Directions, 2023 (SYNTHETIC)",
         ref_no="RBI/2024-25/40", dept_ref="DOR.AML.REC.No.40/14.01.001/2024-25", date_str="June 5, 2024",
         addressee="All Regulated Entities",
         body="""<p>1. Please refer to the Master Direction DOR.AML.REC.No.18/14.01.001/2022-23 dated January 10, 2023.</p>
<p>2. It has been decided to amend paragraph 3 of the said Direction so that low risk customers may also complete periodic updation of KYC through video based customer identification process (V-CIP).</p>
<p>3. These amendments shall come into force with effect from July 1, 2024.</p>"""),
]


def write_raw(raw_dir: Path) -> None:
    raw_dir.mkdir(parents=True, exist_ok=True)
    for d in DOCS:
        (raw_dir / f"ids_{d['num']}.html").write_text(PAGE.format(**d), encoding="utf-8")
        (raw_dir / f"ids_{d['num']}.json").write_text(
            json.dumps({"source": "ids", "num": d["num"], "url": f"https://example.invalid/{d['num']}", "listing_title": ""}),
            encoding="utf-8")
