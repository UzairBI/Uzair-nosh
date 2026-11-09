"""Download and unzip the Recipe Box dataset (three JSON files) into ./data."""
import os
import urllib.request
import zipfile

URL = "https://eightportions.com/recipes_raw.zip"
data = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data")
os.makedirs(data, exist_ok=True)
zpath = os.path.join(data, "recipes_raw.zip")
if not os.path.exists(zpath):
    print("downloading", URL)
    urllib.request.urlretrieve(URL, zpath)
with zipfile.ZipFile(zpath) as z:
    z.extractall(data)
print("ok:", sorted(f for f in os.listdir(data) if f.endswith(".json")))
