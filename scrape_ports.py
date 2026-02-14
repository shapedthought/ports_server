"""
Scrape Veeam help center pages for port data and write to SQLite database.

Usage:
    python scrape_ports.py                  # scrape all products in configuration.json
    python scrape_ports.py --dry-run        # scrape but don't write to DB, print summary
    python scrape_ports.py --db myports.db  # write to a specific DB file
"""

import argparse
import io
import json
import logging
import os
import sqlite3
import sys

import numpy as np
import pandas as pd
import requests
from bs4 import BeautifulSoup

logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
log = logging.getLogger(__name__)

DB_PATH = os.environ.get("DB_PATH", "allports_updated.db")
CONFIG_PATH = os.environ.get("CONFIG_PATH", "configuration.json")

# Column rename mapping: scraped HTML column names → DB column names
COLUMN_RENAME = {
    "Product": "product",
    "Subheading": "subheading",
    "Subheading_L2": "subheadingL2",
    "Subheading_L3": "subheadingL3",
    "From": "sourceService",
    "To": "targetService",
    "Protocol": "protocol",
    "Port": "port",
    "Description": "description",
}

# Rows where "Port" value is actually a section header, not a real port
JUNK_PORT_VALUES = [
    "Other Communications",
    "Communication with Backup Server",
    "Communication with Backup Infrastructure Components",
    "Depends on device configuration",
    "Communication with Virtualization Servers",
]

DB_COLUMNS = [
    "subheading",
    "subheadingL2",
    "subheadingL3",
    "product",
    "sourceService",
    "targetService",
    "protocol",
    "port",
    "description",
]


def load_config(config_path: str) -> list[dict]:
    with open(config_path) as f:
        return json.load(f)


def scrape_product(product: str, url: str) -> list[pd.DataFrame]:
    """Scrape all port tables from a single Veeam help center page."""
    resp = requests.get(url, timeout=30)
    resp.raise_for_status()
    soup = BeautifulSoup(resp.content, "html.parser")

    current_subheading = ""
    current_subheading_l2 = ""
    current_subheading_l3 = ""
    product_dfs = []

    if soup.body is None:
        log.warning("No <body> tag found while scraping product '%s' from %s", product, url)
        return product_dfs

    for element in soup.body.find_all(["span", "table"]):
        if element.name == "span" and "class" in element.attrs:
            classes = element.get("class", [])
            if "Subheading" in classes:
                current_subheading = element.get_text(strip=True)
                current_subheading_l2 = ""
                current_subheading_l3 = ""
            elif "Subheading_L2" in classes:
                current_subheading_l2 = element.get_text(strip=True)
                current_subheading_l3 = ""
            elif "Subheading_L3" in classes:
                current_subheading_l3 = element.get_text(strip=True)
        elif element.name == "table":
            df = pd.read_html(io.StringIO(str(element)))[0]
            df["Subheading"] = current_subheading
            df["Subheading_L2"] = current_subheading_l2
            df["Subheading_L3"] = current_subheading_l3
            df["Product"] = product
            product_dfs.append(df)

    return product_dfs


def clean_dataframe(df: pd.DataFrame) -> pd.DataFrame:
    """Clean and normalize the scraped data."""
    # Merge Notes into Description where Notes exists
    if "Notes" in df.columns:
        df["Description"] = np.where(
            df["Notes"].notna(), df["Notes"], df["Description"]
        )

    df["Description"] = df["Description"].fillna("")

    # Use From value as Subheading fallback when empty
    if "From" in df.columns:
        df["Subheading"] = np.where(
            df["Subheading"] == "", df["From"], df["Subheading"]
        )

    # Merge Port/Endpoint into Port where Port is missing
    if "Port/Endpoint" in df.columns:
        df["Port"] = np.where(
            (df["Port"].isna()) & (df["Port/Endpoint"].notna()),
            df["Port/Endpoint"],
            df["Port"],
        )

    # Drop extra columns that some pages have
    columns_to_drop = [0, "Port/Endpoint", "Notes"]
    df = df.drop(columns=columns_to_drop, errors="ignore")

    # Drop rows with no port value
    df = df.dropna(subset=["Port"])

    # Clean port values
    df["Port"] = df["Port"].astype(str)
    df = df[~df["Port"].isin(JUNK_PORT_VALUES)]
    df["Port"] = df["Port"].str.split("(").str[0].str.strip()

    # Rename columns to match DB schema
    df = df.rename(columns=COLUMN_RENAME)

    # Keep only the DB columns (drop any extras)
    df = df[[c for c in DB_COLUMNS if c in df.columns]]

    # Fill any remaining NaN with empty string
    df = df.fillna("")

    return df


def write_to_db(df: pd.DataFrame, db_path: str) -> None:
    """Drop and recreate the all_ports table, then insert all rows."""
    con = sqlite3.connect(db_path)
    cur = con.cursor()
    cur.execute("DROP TABLE IF EXISTS all_ports")
    cur.execute(
        """
        CREATE TABLE all_ports (
            subheading TEXT,
            subheadingL2 TEXT,
            subheadingL3 TEXT,
            product TEXT,
            sourceService TEXT,
            targetService TEXT,
            protocol TEXT,
            port TEXT,
            description TEXT
        )
        """
    )
    data = df[DB_COLUMNS].values.tolist()
    cur.executemany(
        "INSERT INTO all_ports VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        data,
    )
    con.commit()
    con.close()
    log.info("Wrote %d rows to %s", len(data), db_path)


def main():
    parser = argparse.ArgumentParser(description="Scrape Veeam port data")
    parser.add_argument("--dry-run", action="store_true", help="Scrape but don't write to DB")
    parser.add_argument("--db", default=DB_PATH, help=f"Database path (default: {DB_PATH})")
    parser.add_argument("--config", default=CONFIG_PATH, help=f"Config path (default: {CONFIG_PATH})")
    args = parser.parse_args()

    config = load_config(args.config)
    log.info("Loaded %d products from %s", len(config), args.config)

    all_dfs = []
    for entry in config:
        product = entry["product"]
        url = entry["url"]
        try:
            product_dfs = scrape_product(product, url)
            if product_dfs:
                combined = pd.concat(product_dfs, ignore_index=True)
                all_dfs.append(combined)
                log.info("  %s: %d rows from %d tables", product, len(combined), len(product_dfs))
            else:
                log.warning("  %s: no tables found", product)
        except Exception as e:
            log.error("  %s: failed - %s", product, e)

    if not all_dfs:
        log.error("No data scraped. Exiting.")
        sys.exit(1)

    df = pd.concat(all_dfs, ignore_index=True)
    log.info("Total scraped: %d rows", len(df))

    df = clean_dataframe(df)
    log.info("After cleaning: %d rows", len(df))

    if args.dry_run:
        log.info("Dry run — not writing to DB")
        print(f"\nProducts: {df['product'].nunique()}")
        print(f"Rows per product:")
        print(df["product"].value_counts().to_string())
    else:
        write_to_db(df, args.db)


if __name__ == "__main__":
    main()
