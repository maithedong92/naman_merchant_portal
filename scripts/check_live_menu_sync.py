import asyncio
import json
import sys
import httpx

if hasattr(sys.stdout, 'reconfigure'):
    sys.stdout.reconfigure(encoding='utf-8')


BASE_URL = "https://ump.namanmarket.com"
ADMIN_USER = "admin"
ADMIN_PASS = "phhthtnkhk"

async def main():
    async with httpx.AsyncClient(timeout=30.0, verify=False) as client:
        # 1. Login
        login_res = await client.post(
            f"{BASE_URL}/api/v1/auth/login",
            json={"username": ADMIN_USER, "password": ADMIN_PASS}
        )
        if login_res.status_code != 200:
            print("Login failed:", login_res.text)
            return
        token = login_res.json()["data"]["access_token"]
        headers = {"Authorization": f"Bearer {token}"}

        # 2. Channels
        print("\n=== 1. CHANNELS & CONFIG ===")
        ch_res = await client.get(f"{BASE_URL}/api/v1/channels", headers=headers)
        channels = ch_res.json().get("data", [])
        for ch in channels:
            print(f"- Code: {ch.get('code')}, Name: {ch.get('name')}, Active: {ch.get('is_active')}")
            print(f"  Config: {ch.get('config')}")

        # 3. Stores & Channel Mappings
        print("\n=== 2. STORES & CHANNEL MAPPINGS ===")
        map_res = await client.get(f"{BASE_URL}/api/v1/stores/mappings", headers=headers)
        mappings = map_res.json().get("data", [])
        print(f"Total Store Channel Mappings: {len(mappings)}")
        for m in mappings:
            print(f"  * Store: {m.get('store_code')} ({m.get('store_name')}) -> Channel: {m.get('channel_code')} | PartnerStoreID: {m.get('partner_store_id')} | Active: {m.get('is_active')}")

        # 4. Products Count
        print("\n=== 3. PRODUCT CATALOG IN DB ===")
        prod_res = await client.get(f"{BASE_URL}/api/v1/products?limit=10", headers=headers)
        prod_data = prod_res.json().get("data", {})
        print(f"Products response: {prod_data.get('total') if isinstance(prod_data, dict) else len(prod_data)}")
        if isinstance(prod_data, dict) and "items" in prod_data:
            print(f"Total products in DB: {prod_data.get('total')}")
            for p in prod_data.get("items", [])[:3]:
                print(f"  - SKU: {p.get('sku')}, Name: {p.get('name')}, Price: {p.get('base_price')}")
        elif isinstance(prod_data, list):
            print(f"Total products returned: {len(prod_data)}")
            for p in prod_data[:3]:
                print(f"  - SKU: {p.get('sku')}, Name: {p.get('name')}, Price: {p.get('base_price')}")

        # 5. Check ShopeeFood Menu cache / endpoint
        print("\n=== 4. TEST MENU SYNC SIMULATION / CALL ===")
        stores_res = await client.get(f"{BASE_URL}/api/v1/stores", headers=headers)
        stores = stores_res.json().get("data", [])
        for store in stores:
            st_id = store.get("id")
            st_code = store.get("code")
            print(f"\n--- Testing Store {st_code} ({store.get('name')}) ---")
            
            # ShopeeFood with HTTP Basic Auth
            sf_res = await client.get(
                f"{BASE_URL}/shopeefoodapi/{st_code}.json",
                auth=("namanmarket", "shopeefoodapi")
            )
            print(f"  [ShopeeFood] GET /shopeefoodapi/{st_code}.json -> Status: {sf_res.status_code}")
            if sf_res.status_code == 200:
                sf_json = sf_res.json()
                cats = sf_json.get("sections", [{}])[0].get("categories", [])
                item_count = sum(len(c.get("items", [])) for c in cats)
                print(f"    -> ShopeeFood catalog valid! Sections: {len(sf_json.get('sections', []))}, Categories: {len(cats)}, Total items: {item_count}")
                if cats:
                    print(f"    -> Sample Cat: {cats[0].get('name')} ({len(cats[0].get('items', []))} items)")
            else:
                print(f"    -> Body: {sf_res.text[:200]}")

            # GrabMart
            gm_res = await client.get(f"{BASE_URL}/grabmart/menu?merchantID={st_code}&partnerMerchantID={st_code}")
            print(f"  [GrabMart] GET /grabmart/menu -> Status: {gm_res.status_code}")
            if gm_res.status_code == 200:
                gm_json = gm_res.json()
                cats = gm_json.get("categories", [])
                item_count = sum(len(sub.get("items", [])) for c in cats for sub in c.get("subcategories", []))
                print(f"    -> GrabMart catalog valid! Categories: {len(cats)}, Total items: {item_count}")
                if cats:
                    print(f"    -> Sample Cat: {cats[0].get('name')} (Subcats: {len(cats[0].get('subcategories', []))})")
            else:
                print(f"    -> Body: {gm_res.text[:200]}")

        # ShopeeMart connection status
        print("\n--- ShopeeMart Connection Status ---")
        sm_res = await client.get(f"{BASE_URL}/shopeemart/connection-status", headers=headers)
        print(f"  [ShopeeMart] GET /shopeemart/connection-status -> Status: {sm_res.status_code}, Body: {sm_res.text}")


if __name__ == "__main__":
    asyncio.run(main())
