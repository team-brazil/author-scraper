#!/usr/bin/env python
# coding: utf-8

# In[2]:


import requests
import pandas as pd
import time
import os

# Directory to save individual files
output_dir = "openalex_field_outputs"
os.makedirs(output_dir, exist_ok=True)

def fetch_researchers_onefile(field_id, field_name, max_authors=50000):
    base_url = "https://api.openalex.org/authors"
    cursor = "*"
    per_page = 200
    authors = []
    downloaded = 0

    print(f"📥 Starting: {field_name} — Max: {max_authors}")

    while downloaded < max_authors:
        params = {
            "filter": f"x_concepts.id:{field_id}",
            "per-page": per_page,
            "cursor": cursor
        }
        response = requests.get(base_url, params=params)
        if response.status_code != 200:
            print(f"❌ Request failed for {field_name}: {response.status_code}")
            break

        data = response.json()
        results = data.get("results", [])
        if not results:
            break

        for author in results:
            institution = author.get("last_known_institution", {})
            authors.append({
                "name": author.get("display_name"),
                "orcid": author.get("orcid"),
                "institution_id": institution.get("id", "N/A"),
                "affiliation": institution.get("display_name", "N/A"),
                "country": institution.get("country_code", "N/A"),
                "works_count": author.get("works_count", 0),
                "cited_by_count": author.get("cited_by_count", 0),
                "fields": "; ".join([c["display_name"] for c in author.get("x_concepts", [])]),
                "field_group": field_name
            })

        downloaded += len(results)
        print(f"📊 {field_name}: Downloaded {downloaded}")

        cursor = data.get("meta", {}).get("next_cursor")
        if not cursor:
            break

        time.sleep(1)  # Respect OpenAlex rate limits

    if authors:
        df = pd.DataFrame(authors)
        file_path = os.path.join(output_dir, f"{field_name.replace(' ', '_').lower()}_researchers.csv")
        df.to_csv(file_path, index=False)
        print(f"✅ Saved {len(df)} researchers to: {file_path}")
        return file_path
    else:
        print(f"⚠️ No data found for {field_name}")
        return None

# === Broad Fields with OpenAlex Concept IDs ===
fields = {
    "Computer Science": "C41008148",
    "Medicine": "C71924100",
    "Physics": "C121332964",
    "Chemistry": "C185592680",
    "Biology": "C154945302",
    "Environmental Science": "C127413603",
    "Materials Science": "C86803240",
    "Psychology": "C15744967",
    "Economics": "C162324750",
    "Social Sciences": "C2778407487"
}

# === Run for each field and collect file paths ===
all_csv_paths = []

for field_name, concept_id in fields.items():
    file_path = fetch_researchers_onefile(field_id=concept_id, field_name=field_name, max_authors=50000)
    if file_path:
        all_csv_paths.append(file_path)

# === Merge all into one final file ===
print("\n🔗 Merging all field files into one master CSV...")

merged_df = pd.concat([pd.read_csv(path) for path in all_csv_paths], ignore_index=True)
merged_filename = "all_researchers_merged.csv"
merged_df.to_csv(merged_filename, index=False)

print(f"✅ All done! Merged file saved as: {merged_filename}")


# In[ ]:




