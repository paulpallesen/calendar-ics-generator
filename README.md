# Calendar ICS Generator

This repository generates ICS calendar feeds from a Google Sheet (published as CSV) and deploys them to GitHub Pages. It includes a subscribe webpage for easy addition to calendar apps.

## Setup

1. **Publish Google Sheet to CSV**:
   - Your sheet is already published at: https://docs.google.com/spreadsheets/d/e/2PACX-1vRwoBRBFI-z4haeZv0WMruMzGmebRVPIa-pYOzbMVBX9Si9iQa8VMBzkoYjWt8VRck6OB9853xSSPzM/pub?gid=0&single=true&output=csv
   - The workflow is pre-configured with this URL.

2. **Enable GitHub Pages**:
   - Go to repo Settings > Pages.
   - Source: "Deploy from a branch" > main > / (root) > Save.
   - The site will be at `https://<username>.github.io/<repo>/`.

## Running Builds

- **Manual**: Actions tab > Build Calendars > Run workflow > Select "manual" > Run.
- **Automated Cadence**:
  - Edit `.github/workflows/build-calendars.yml`.
  - For weekly: Uncomment `- cron: '0 0 * * 0'`.
  - For monthly: Uncomment `- cron: '0 0 1 * *'`.
  - For manual only: Ensure both cron lines are commented.
  - Commit to enable the selected cadence.

Builds also trigger on pushes to `build_calendars.py`, `requirements.txt`, or the workflow file.

## Local Preview and Testing

1. **Install Dependencies**:
