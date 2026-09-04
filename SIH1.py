import math
import time
from typing import Dict, Tuple
from urllib.parse import quote_plus
from geopy.geocoders import Nominatim
import pandas as pd
import requests
from sqlalchemy import create_engine

# =============================================================================
# 1. DATABASE CONFIGURATION
# =============================================================================
DB_USER = "postgres"
DB_PASSWORD = "D@30&D@22"  # Replace with your actual pgAdmin password
DB_HOST = "localhost"
DB_PORT = "5432"
DB_NAME = "agri_db"

encoded_password = quote_plus(DB_PASSWORD)
DATABASE_URL = (
    f"postgresql://{DB_USER}:{encoded_password}@{DB_HOST}:{DB_PORT}/{DB_NAME}"
)
engine = create_engine(DATABASE_URL)


# =============================================================================
# 2. OPTIONAL: AGMARKNET API & GEOCODING ETL (Uses requests, geopy, time)
# =============================================================================
def fetch_and_store_agmarknet_data(api_key: str, limit: int = 20):
  """Fetches live market data via requests, geocodes via geopy, and saves to DB."""
  url = f"https://api.data.gov.in/resource/9ef74138-9624-4631-8522-ad449ebd01f4?api-key={api_key}&format=json&limit={limit}"

  try:
    response = requests.get(url, timeout=10)
    if response.status_code == 200:
      records = response.json().get("records", [])
      df = pd.DataFrame(records)

      # Geocode missing lat/lon using geopy
      geolocator = Nominatim(user_agent="agri_matcher")
      lats, lons = [], []
      for _, row in df.iterrows():
        query = f"{row.get('market')}, {row.get('district')}, India"
        loc = geolocator.geocode(query, timeout=5)
        time.sleep(1)  # Uses time for rate limiting
        lats.append(loc.latitude if loc else 0.0)
        lons.append(loc.longitude if loc else 0.0)

      df["lat"], df["lon"] = lats, lons
      df.to_sql(
          "agmarknet_mandi_prices", engine, if_exists="replace", index=False
      )
      print(" Agmarknet market data updated in PostgreSQL!")
  except Exception as e:
    print(f"API/Geocoding skipped: {e}")


# =============================================================================
# 3. MAIN DATABASE RETRIEVAL (Uses pandas, sqlalchemy, urllib.parse)
# =============================================================================
def fetch_pgadmin_tables() -> Tuple[pd.DataFrame, pd.DataFrame]:
  """Queries farmer_lots and buyer_demands tables directly from PostgreSQL 18."""
  farmer_lots_df = pd.read_sql("SELECT * FROM farmer_lots;", engine)
  buyer_demands_df = pd.read_sql("SELECT * FROM buyer_demands;", engine)
  return farmer_lots_df, buyer_demands_df


# =============================================================================
# 4. SMART MATCHING ENGINE (Uses math, typing)
# =============================================================================
class SmartFarmerBuyerMatcher:

  def __init__(
      self,
      w_price: float = 0.35,
      w_dist: float = 0.25,
      w_trust: float = 0.20,
      w_qty: float = 0.20,
      max_distance_km: float = 300.0,
      transport_cost_per_tonne_km: float = 3.5,
  ):
    self.w_p, self.w_d, self.w_t, self.w_q = (
        w_price,
        w_dist,
        w_trust,
        w_qty,
    )
    self.max_d = max_distance_km
    self.transport_cost_per_tonne_km = transport_cost_per_tonne_km

  @staticmethod
  def haversine_distance(
      lat1: float, lon1: float, lat2: float, lon2: float
  ) -> float:
    R = 6371.0
    dlat, dlon = math.radians(lat2 - lat1), math.radians(lon2 - lon1)
    a = math.sin(dlat / 2) ** 2 + math.cos(math.radians(lat1)) * math.cos(
        math.radians(lat2)
    ) * math.sin(dlon / 2) ** 2
    return R * (2 * math.asin(math.sqrt(a)))

  def compute_match(
      self, lot: pd.Series, buyer: pd.Series
  ) -> Dict[str, float]:
    dist_km = self.haversine_distance(
        lot["lat"], lot["lon"], buyer["lat"], buyer["lon"]
    )

    s_dist = max(0.0, 100.0 * (1.0 - (dist_km / self.max_d)))
    s_price = (
        min(100.0, (buyer["offered_price"] / lot["expected_price"]) * 100.0)
        if lot["expected_price"] > 0
        else 0.0
    )
    s_qty = (
        min(lot["quantity_tonnes"], buyer["req_quantity"])
        / max(lot["quantity_tonnes"], buyer["req_quantity"])
    ) * 100.0
    s_trust = float(buyer["trust_score"])

    total_score = (
        (self.w_p * s_price)
        + (self.w_d * s_dist)
        + (self.w_t * s_trust)
        + (self.w_q * s_qty)
    )
    net_price_per_quintal = buyer["offered_price"] - (
        (dist_km * self.transport_cost_per_tonne_km) / 10.0
    )

    return {
        "buyer_id": buyer["buyer_id"],
        "buyer_type": buyer["buyer_type"],
        "offered_price": buyer["offered_price"],
        "distance_km": round(dist_km, 1),
        "trust_pct": f"{round(s_trust, 1)}%",
        "match_score": round(total_score, 2),
        "net_price_quintal": round(net_price_per_quintal, 2),
    }

  def match_lot(
      self,
      lot_id: str,
      lots_df: pd.DataFrame,
      buyers_df: pd.DataFrame,
      top_n: int = 3,
  ) -> pd.DataFrame:
    lot = lots_df[lots_df["lot_id"] == lot_id].iloc[0]
    eligible_buyers = buyers_df[
        (buyers_df["req_crop"].str.lower() == lot["crop"].lower())
        & (buyers_df["req_grade"].str.upper() == lot["grade"].upper())
    ]
    results = [
        self.compute_match(lot, buyer)
        for _, buyer in eligible_buyers.iterrows()
    ]
    return pd.DataFrame(results).sort_values(
        by="match_score", ascending=False
    ).head(top_n)


# =============================================================================
# 5. PIPELINE EXECUTION
# =============================================================================
if __name__ == "__main__":
  print("1. Fetching live tables directly from PostgreSQL...")
  farmer_lots, buyer_demands = fetch_pgadmin_tables()

  print("2. Running Matching Engine...")
  matcher = SmartFarmerBuyerMatcher()
  recommendations = matcher.match_lot("WB-POT-001", farmer_lots, buyer_demands)

  print("\n=== MATCHING RECOMMENDATIONS FOR LOT: WB-POT-001 ===")
  print(recommendations.to_string(index=False))
