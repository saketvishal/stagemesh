"""Browser-level error policy: terminal errors stop polling, transient failures remain retryable."""
from pathlib import Path
import pytest
import test_ui_browser_behavior as browser
from test_ui_browser_behavior import fixture_base

DRIVER = r'''
(() => {
  const scenario = new URLSearchParams(location.search).get("scenario");
  const status = Number(scenario);
  const original = window.fetch.bind(window);
  const out = {errors: []};
  let failing = false, calls = 0;
  window.fetch = async (...args) => {
    if (String(args[0]).startsWith("/api/v1/activity")) {
      calls++;
      if (failing && status !== 400) {
        if (status === 0) throw new TypeError("controlled network loss");
        return new Response(JSON.stringify({contract: "stagemesh.activity/1", error: {code: "controlled", message: "controlled " + status}}), {status, headers: {"Content-Type": "application/json"}});
      }
    }
    return original(...args);
  };
  const $ = id => document.getElementById(id);
  const sleep = n => new Promise(resolve => setTimeout(resolve, n));
  const rows = () => document.querySelectorAll("#rows tr").length;
  const until = async (fn, label) => { for (let i=0; i<400; i++) {if(fn()) return; await sleep(25);} throw Error("timeout: " + label); };
  window.addEventListener("error", e => out.errors.push(e.message));
  window.addEventListener("load", async () => {
    try {
      await until(() => rows() === 100, "initial real response");
      failing = true;
      if (status === 400) {
        $("period").value = "custom";
        $("since").value = "2027-01-02T00:00";
        $("until").value = "2026-01-01T00:00";
        $("since").dispatchEvent(new Event("change"));
      } else $("refresh").click();
      await until(() => $("message").textContent.includes("Could not load activity"), "error displayed");
      out.rowsAfterError = rows();
      out.errorMessage = $("message").textContent;
      out.autoAfterError = $("auto").checked;
      const afterError = calls;
      await sleep(22000);
      out.automaticRetries = calls - afterError;
      failing = false;
      $("period").value = "all";
      $("period").dispatchEvent(new Event("change"));
      await until(() => rows() === 100 && $("message").textContent === "", "explicit corrected-filter recovery");
      out.autoAfterRecovery = $("auto").checked;
      out.rowsAfterRecovery = rows();
      const beforeResume = calls;
      $("auto").checked = true;
      $("auto").dispatchEvent(new Event("change"));
      await sleep(11000);
      out.explicitlyResumedPolls = calls - beforeResume;
    } catch (err) {out.errors.push(String(err));}
    const result = document.createElement("pre");result.id = "result";result.textContent = JSON.stringify(out);document.body.append(result);
  });
})();
'''

@pytest.mark.parametrize("status", [400, 401, 403, 503, 0], ids=["invalid-range", "unauthenticated", "forbidden", "store-unavailable", "network-loss"])
def test_browser_polling_obeys_error_policy_and_explicit_recovery(fixture_base, tmp_path: Path, status):
    result = browser.run_scenario(fixture_base, str(status), tmp_path)
    print("ERROR_POLICY_BROWSER_FACTS=" + str(result))
    terminal = status in (400, 401, 403)
    assert result["autoAfterError"] is not terminal, result
    assert (result["automaticRetries"] == 0 if terminal else result["automaticRetries"] >= 1), result
    assert result["rowsAfterError"] == (0 if status in (401, 403) else 100), result
    if status in (400, 503, 0):
        assert "stale" in result["errorMessage"], result
    assert result["autoAfterRecovery"] is not terminal, result
    assert result["rowsAfterRecovery"] == 100 and result["explicitlyResumedPolls"] >= 1, result

@pytest.fixture(autouse=True)
def policy_driver(monkeypatch):
    monkeypatch.setattr(browser, "DRIVER", DRIVER)
