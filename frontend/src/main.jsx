import React, { useEffect, useState } from "react";
import { createRoot } from "react-dom/client";
import "./style.css";
import "./layout.css";

let csrf = "";
const request = async (path, options = {}) => {
  const normalizedPath = path && !path.endsWith("/") ? `${path}/` : path;
  const response = await fetch(`/api/v1/${normalizedPath}`, {
    credentials: "same-origin",
    headers: {
      "Content-Type": "application/json",
      ...(options.method && options.method !== "GET"
        ? { "X-CSRFToken": csrf }
        : {}),
    },
    ...options,
  });
  const isJson = response.headers
    .get("content-type")
    ?.includes("application/json");
  const body = isJson
    ? await response.json()
    : {
        error:
          "The server returned an unexpected response. Reload the page and try again.",
      };
  if (!response.ok) throw new Error(body.error || "Request failed.");
  return body;
};
const formatTime = (value) =>
  value
    ? new Intl.DateTimeFormat("en-CA", {
        dateStyle: "medium",
        timeStyle: "short",
      }).format(new Date(value))
    : "Not checked yet";
const formatPostalCode = (value) => {
  const compact = value
    .toUpperCase()
    .replace(/[^A-Z0-9]/g, "")
    .slice(0, 6);
  return compact.length > 3
    ? `${compact.slice(0, 3)} ${compact.slice(3)}`
    : compact;
};
const validCanadianPostalCode = (value) =>
  /^[ABCEGHJKLMNPRSTVXY]\d[ABCEGHJKLMNPRSTVWXYZ]\d[ABCEGHJKLMNPRSTVWXYZ]\d$/.test(
    value.replace(/\s/g, ""),
  );

function Auth({ onReady }) {
  const [mode, setMode] = useState("login"),
    [message, setMessage] = useState(""),
    [form, setForm] = useState({ email: "", password: "", displayName: "" });
  const submit = async (event) => {
    event.preventDefault();
    setMessage("");
    try {
      const data = await request(mode, {
        method: "POST",
        body: JSON.stringify(form),
      });
      if (mode === "login") onReady(data.user);
      else {
        setMessage(data.message);
        setMode("login");
      }
    } catch (error) {
      setMessage(error.message);
    }
  };
  return (
    <main className="auth-shell">
      <section className="brand-panel">
        <p className="eyebrow">STOCK INTELLIGENCE</p>
        <h1>Never miss the next drop.</h1>
        <p>
          Quietly track Nintendo Canada products and receive a Discord alert
          when shipping stock returns.
        </p>
        <div className="signal">
          <span></span> Monitoring Canadian retail, one restock at a time.
        </div>
      </section>
      <section className="auth-card">
        <p className="eyebrow">
          {mode === "login" ? "WELCOME BACK" : "CREATE ACCOUNT"}
        </p>
        <h2>
          {mode === "login" ? "Sign in to StockBot" : "Join the watchlist"}
        </h2>
        <form onSubmit={submit}>
          {mode === "register" && (
            <label>
              Display name
              <input
                value={form.displayName}
                onChange={(e) =>
                  setForm({ ...form, displayName: e.target.value })
                }
              />
            </label>
          )}
          <label>
            Email
            <input
              type="email"
              required
              value={form.email}
              onChange={(e) => setForm({ ...form, email: e.target.value })}
            />
          </label>
          <label>
            Password
            <input
              type="password"
              minLength="10"
              required
              value={form.password}
              onChange={(e) => setForm({ ...form, password: e.target.value })}
            />
          </label>
          <button>{mode === "login" ? "Sign in" : "Create account"}</button>
        </form>
        {message && <p className="notice">{message}</p>}
        <button
          className="link"
          onClick={() => {
            setMode(mode === "login" ? "register" : "login");
            setMessage("");
          }}
        >
          {mode === "login"
            ? "Need an account? Register"
            : "Already registered? Sign in"}
        </button>
        <p className="fine">
          The first registered account becomes the administrator. Later accounts
          require approval.
        </p>
      </section>
    </main>
  );
}

function AddMonitor({ webhooks, onCreated }) {
  const [url, setUrl] = useState(""),
    [selectedStore, setSelectedStore] = useState(""),
    [applePartNumber, setApplePartNumber] = useState(""),
    [appleCatalog, setAppleCatalog] = useState([]),
    [postalCode, setPostalCode] = useState(""),
    [fulfillment, setFulfillment] = useState("shipping"),
    [nearbyStores, setNearbyStores] = useState([]),
    [locationKeys, setLocationKeys] = useState([]),
    [preview, setPreview] = useState(null),
    [webhookId, setWebhookId] = useState(webhooks[0]?.id || ""),
    [message, setMessage] = useState(""),
    [loading, setLoading] = useState(false);
  const isAppleUrl =
    selectedStore === "apple_ca" ||
    /^https?:\/\/(?:www\.)?apple\.com\/ca\/shop\//i.test(url.trim());
  const needsPostalCode =
    selectedStore === "bestbuy_ca" || selectedStore === "apple_ca";
  const postalCodeIsValid = validCanadianPostalCode(postalCode);
  useEffect(() => {
    if (!isAppleUrl || appleCatalog.length) return;
    request("apple-catalog/")
      .then((data) => setAppleCatalog(data.items || []))
      .catch((error) => setMessage(error.message));
  }, [isAppleUrl, appleCatalog.length]);
  useEffect(() => {
    const input = document.querySelector(
      'input[aria-label="Canadian postal code"]',
    );
    const hint = document.querySelector(".postal-setup .fine");
    if (input) input.placeholder = "A1A 1A1";
    if (hint)
      hint.textContent =
        "Enter all six characters in Canadian postal-code format.";
  }, [needsPostalCode, postalCodeIsValid]);
  useEffect(() => {
    return;
    const form = document.querySelector(".add form");
    const existing = document.getElementById("apple-part-number");
    if (!isAppleUrl) {
      existing?.closest("label")?.remove();
      return;
    }
    if (existing || !form) return;
    const label = document.createElement("label");
    label.className = "field-label";
    label.textContent = "Apple Canada Order No.";
    const input = document.createElement("input");
    input.id = "apple-part-number";
    input.placeholder = "Example: MG854VC/A";
    input.autocomplete = "off";
    input.addEventListener("input", (event) =>
      setApplePartNumber(event.target.value.toUpperCase()),
    );
    label.appendChild(input);
    form.insertBefore(
      label,
      form.querySelector("fieldset") || form.querySelector("button"),
    );
  }, [isAppleUrl]);
  useEffect(() => {
    return;
    const form = document.querySelector(".add form");
    const existing = document.getElementById("store-selector");
    if (!form || existing) return;
    const label = document.createElement("label");
    label.className = "field-label";
    label.textContent = "Store";
    const select = document.createElement("select");
    select.id = "store-selector";
    select.appendChild(new Option("Choose a store", ""));
    select.appendChild(new Option("Nintendo Canada", "nintendo_ca"));
    select.appendChild(new Option("Best Buy Canada", "bestbuy_ca"));
    select.appendChild(new Option("Apple Canada", "apple_ca"));
    select.addEventListener("change", (event) => {
      setSelectedStore(event.target.value);
      const productInput = document.querySelector(
        'input[aria-label="Product URL"]',
      );
      if (productInput) {
        productInput.required = event.target.value !== "apple_ca";
        productInput.closest("label").style.display =
          event.target.value === "apple_ca" ? "none" : "";
      }
    });
    form.insertBefore(label, form.firstChild);
  }, []);
  useEffect(() => {
    return;
    const form = document.querySelector(".add form");
    const existing = document.getElementById("apple-catalog-select");
    if (!isAppleUrl) {
      existing?.closest("label")?.remove();
      return;
    }
    if (existing || !form) return;
    fetch("/api/v1/apple-catalog/", { credentials: "same-origin" })
      .then((response) => response.json())
      .then((data) => {
        if (
          !data.items?.length ||
          document.getElementById("apple-catalog-select")
        )
          return;
        const label = document.createElement("label");
        label.className = "field-label";
        label.textContent = "Known Apple Canada configuration";
        const select = document.createElement("select");
        select.id = "apple-catalog-select";
        select.appendChild(
          new Option("Select a known configuration (optional)", ""),
        );
        data.items.forEach((item) =>
          select.appendChild(
            new Option(`${item.title} - ${item.orderNumber}`, item.orderNumber),
          ),
        );
        select.addEventListener("change", (event) => {
          setApplePartNumber(event.target.value);
          const part = document.getElementById("apple-part-number");
          if (part) part.value = event.target.value;
        });
        label.appendChild(select);
        form.insertBefore(
          label,
          document.getElementById("apple-part-number")?.closest("label") ||
            form.querySelector("fieldset") ||
            form.querySelector("button"),
        );
      });
  }, [isAppleUrl]);
  const findStores = async () => {
    setLoading(true);
    setMessage("");
    try {
      const data = await request(
        selectedStore === "apple_ca" ? "apple-stores/" : "bestbuy-stores/",
        {
          method: "POST",
          body: JSON.stringify({ postalCode, applePartNumber }),
        },
      );
      setNearbyStores(data.stores);
      setLocationKeys([]);
    } catch (error) {
      setMessage(error.message);
    } finally {
      setLoading(false);
    }
  };
  const validate = async (e) => {
    e.preventDefault();
    setLoading(true);
    setMessage("");
    try {
      const targetUrl =
        selectedStore === "apple_ca" ? "https://www.apple.com/ca/shop/" : url;
      const data = await request("validate/", {
        method: "POST",
        body: JSON.stringify({
          url: targetUrl,
          applePartNumber,
          postalCode,
          fulfillment,
          locationKeys,
        }),
      });
      setPreview(data.validation);
    } catch (error) {
      setMessage(error.message);
    } finally {
      setLoading(false);
    }
  };
  const confirm = async () => {
    try {
      await request("confirm-monitor/", {
        method: "POST",
        body: JSON.stringify({ validationId: preview.id, webhookId }),
      });
      onCreated();
      setPreview(null);
      setUrl("");
      setPostalCode("");
      setNearbyStores([]);
      setLocationKeys([]);
      setFulfillment("shipping");
    } catch (error) {
      setMessage(error.message);
    }
  };
  const toggleStore = (id) =>
    setLocationKeys((keys) =>
      keys.includes(id) ? keys.filter((key) => key !== id) : [...keys, id],
    );
  return (
    <section className="panel add">
      <div>
        <p className="eyebrow">NEW MONITOR</p>
        <h2>Validate a product</h2>
        <p>
          Add a Nintendo Canada, Best Buy Canada, or Apple Canada monitor to
          check its current availability.
        </p>
      </div>
      <form onSubmit={validate}>
        <label className="field-label">
          Store
          <select
            value={selectedStore}
            onChange={(e) => {
              const store = e.target.value;
              setSelectedStore(store);
              setFulfillment("shipping");
              setNearbyStores([]);
              setLocationKeys([]);
            }}
            required
          >
            <option value="">Choose a store</option>
            <option value="nintendo_ca">Nintendo Canada</option>
            <option value="bestbuy_ca">Best Buy Canada</option>
            <option value="apple_ca">Apple Canada</option>
          </select>
        </label>
        {selectedStore !== "apple_ca" && <label className="field-label">
          Product link
          <input
            aria-label="Product URL"
            placeholder="https://www.bestbuy.ca/en-ca/product/..."
            value={url}
            onChange={(e) => {
              setUrl(e.target.value);
              setNearbyStores([]);
              setLocationKeys([]);
            }}
            required
          />
        </label>}
        {isAppleUrl && <>
          <label className="field-label">
            Known iPhone configuration
            <select
              value={applePartNumber}
              onChange={(e) => {
                setApplePartNumber(e.target.value);
                setNearbyStores([]);
                setLocationKeys([]);
              }}
            >
              <option value="">Select a known configuration</option>
              {appleCatalog.map((item) => <option key={item.orderNumber} value={item.orderNumber}>{item.label}</option>)}
            </select>
          </label>
          <label className="field-label">
            Apple Canada Order No.
            <input
              value={applePartNumber}
              onChange={(e) => {
                setApplePartNumber(e.target.value.toUpperCase());
                setNearbyStores([]);
                setLocationKeys([]);
              }}
              placeholder="Apple order number"
              required
            />
          </label>
        </>}
        {needsPostalCode && (
          <>
            <fieldset className="fulfillment">
              <legend>How would you like to receive it?</legend>
              <label className={fulfillment === "shipping" ? "chosen" : ""}>
                <input
                  type="radio"
                  name="fulfillment"
                  checked={fulfillment === "shipping"}
                  onChange={() => setFulfillment("shipping")}
                />
                <span>
                  <strong>Ship to me</strong>
                  <small>Alert when online shipping is available.</small>
                </span>
              </label>
              <label className={fulfillment === "pickup" ? "chosen" : ""}>
                <input
                  type="radio"
                  name="fulfillment"
                  checked={fulfillment === "pickup"}
                  onChange={() => setFulfillment("pickup")}
                />
                <span>
                  <strong>Pick up in store</strong>
                  <small>Alert when any selected nearby store has stock.</small>
                </span>
              </label>
              <label className={fulfillment === "either" ? "chosen" : ""}>
                <input
                  type="radio"
                  name="fulfillment"
                  checked={fulfillment === "either"}
                  onChange={() => setFulfillment("either")}
                />
                <span>
                  <strong>Either option</strong>
                  <small>
                    Alert for shipping or your selected pickup stores.
                  </small>
                </span>
              </label>
            </fieldset>
            {fulfillment !== "shipping" && (
              <>
                <div className="postal-setup">
                  <label className="field-label">
                    Canadian postal code
                    <input
                      aria-label="Canadian postal code"
                      inputMode="text"
                      autoCapitalize="characters"
                      placeholder="J9H 3V7"
                      value={postalCode}
                      onChange={(e) =>
                        setPostalCode(formatPostalCode(e.target.value))
                      }
                      required
                    />
                  </label>
                  <button
                    type="button"
                    onClick={findStores}
                    disabled={!postalCodeIsValid || loading || (isAppleUrl && !applePartNumber)}
                  >
                    Find nearby stores
                  </button>
                  {postalCode && !postalCodeIsValid && (
                    <p className="fine">
                      Enter all six characters, for example J9H 3V7.
                    </p>
                  )}
                </div>
                <div className="store-list">
                  {nearbyStores.length ? (
                    <>
                      <p className="fine">
                        Select one or more stores to monitor.
                      </p>
                      {nearbyStores.map((store) => (
                        <label key={store.id}>
                          <input
                            type="checkbox"
                            checked={locationKeys.includes(store.id)}
                            onChange={() => toggleStore(store.id)}
                          />
                          <span>
                            <strong>{store.name}</strong> {store.city},{" "}
                            {store.region}{" "}
                            {store.distance != null &&
                              `· ${store.distance.toFixed(1)} km`}
                          </span>
                        </label>
                      ))}
                    </>
                  ) : (
                    <p className="fine">
                      Enter a valid postal code and choose Find nearby stores.
                    </p>
                  )}
                </div>
              </>
            )}
          </>
        )}
        <button disabled={loading}>
          {loading ? "Validating..." : "Validate product"}
        </button>
      </form>
      {message && <p className="notice">{message}</p>}
      {preview && (
        <div className="preview">
          <img src={preview.imageUrl} alt="" />
          <div>
            <strong>{preview.title}</strong>
            <p>
              {preview.price || "Price unavailable"} ·{" "}
              <Status value={preview.availability} />
            </p>
            <select
              value={webhookId}
              onChange={(e) => setWebhookId(e.target.value)}
            >
              <option value="">Select Discord destination</option>
              {webhooks.map((item) => (
                <option key={item.id} value={item.id}>
                  {item.name}
                </option>
              ))}
            </select>
          </div>
          <button onClick={confirm} disabled={!webhookId}>
            Start monitoring
          </button>
        </div>
      )}
    </section>
  );
}

function Status({ value }) {
  return (
    <span className={`status ${value}`}>
      {value === "available"
        ? "Available"
        : value === "unavailable"
          ? "Out of stock"
          : value}
    </span>
  );
}

function Dashboard({ user, logout }) {
  const [monitors, setMonitors] = useState([]),
    [webhooks, setWebhooks] = useState([]),
    [view, setView] = useState("monitors"),
    [webhookForm, setWebhookForm] = useState({ name: "", url: "" }),
    [users, setUsers] = useState([]),
    [message, setMessage] = useState("");
  const load = async () => {
    try {
      const [m, w] = await Promise.all([
        request("monitors/"),
        request("webhooks/"),
      ]);
      setMonitors(m.monitors);
      setWebhooks(w.webhooks);
    } catch (error) {
      setMessage(error.message);
    }
  };
  const loadUsers = async () => {
    const data = await request("staff-users/");
    setUsers(data.users);
  };
  useEffect(() => {
    load();
  }, []);
  const addWebhook = async (e) => {
    e.preventDefault();
    try {
      await request("webhooks/", {
        method: "POST",
        body: JSON.stringify(webhookForm),
      });
      setWebhookForm({ name: "", url: "" });
      load();
    } catch (error) {
      setMessage(error.message);
    }
  };
  const updateMonitor = async (id, active) => {
    await request(`monitors/${id}/`, {
      method: "PATCH",
      body: JSON.stringify({ active }),
    });
    load();
  };
  const updateUser = async (id, action) => {
    await request(`staff-users/${id}/`, {
      method: "PATCH",
      body: JSON.stringify({ action }),
    });
    loadUsers();
  };
  return (
    <main className="app-shell">
      <aside>
        <a className="wordmark">
          STOCK<span>BOT</span>
        </a>
        <nav>
          <button
            className={view === "monitors" ? "selected" : ""}
            onClick={() => setView("monitors")}
          >
            Monitor desk
          </button>
          <button
            className={view === "webhooks" ? "selected" : ""}
            onClick={() => setView("webhooks")}
          >
            Discord destinations
          </button>
          {user.staff && (
            <button
              className={view === "admin" ? "selected" : ""}
              onClick={() => {
                setView("admin");
                loadUsers();
              }}
            >
              Admin control
            </button>
          )}
        </nav>
        <div className="account">
          <strong>{user.displayName || user.email}</strong>
          <small>{user.staff ? "Administrator" : "Approved member"}</small>
          <button className="link" onClick={logout}>
            Sign out
          </button>
        </div>
      </aside>
      <section className="content">
        {message && <p className="notice">{message}</p>}
        {view === "monitors" && (
          <>
            <header>
              <p className="eyebrow">MONITOR DESK</p>
              <h1>Your restock signals</h1>
              <p>
                {monitors.filter((m) => m.active).length} active monitors.
                Nintendo Canada shipping only.
              </p>
            </header>
            <AddMonitor webhooks={webhooks} onCreated={load} />
            <section className="grid">
              {monitors.map((m) => (
                <article className="monitor" key={m.id}>
                  <div className="product-image">
                    {m.product.imageUrl ? (
                      <img src={m.product.imageUrl} alt="" />
                    ) : (
                      "N"
                    )}
                  </div>
                  <div className="monitor-body">
                    <div className="monitor-top">
                      <Status value={m.product.availability} />
                      <button
                        className="link"
                        onClick={() => updateMonitor(m.id, !m.active)}
                      >
                        {m.active ? "Pause" : "Resume"}
                      </button>
                    </div>
                    <h3>{m.product.title}</h3>
                    <p className="price">
                      {m.product.price || "Price unavailable"}
                    </p>
                    <p className="meta">
                      Discord: {m.webhookName}
                      <br />
                      Checked: {formatTime(m.product.lastCheckedAt)}
                    </p>
                    {m.product.lastError && (
                      <p className="warning">{m.product.lastError}</p>
                    )}
                    <a href={m.product.url} target="_blank">
                      View product
                    </a>
                  </div>
                </article>
              ))}
              {!monitors.length && (
                <article className="empty">
                  <h3>Your desk is clear.</h3>
                  <p>
                    Validate a Nintendo Canada link above to start your first
                    monitor.
                  </p>
                </article>
              )}
            </section>
          </>
        )}
        {view === "webhooks" && (
          <>
            <header>
              <p className="eyebrow">DISCORD DESTINATIONS</p>
              <h1>Route restock alerts</h1>
            </header>
            <section className="panel">
              <form onSubmit={addWebhook} className="stack">
                <input
                  placeholder="Name, e.g. Console alerts"
                  value={webhookForm.name}
                  onChange={(e) =>
                    setWebhookForm({ ...webhookForm, name: e.target.value })
                  }
                  required
                />
                <input
                  placeholder="Discord webhook URL"
                  value={webhookForm.url}
                  onChange={(e) =>
                    setWebhookForm({ ...webhookForm, url: e.target.value })
                  }
                  required
                />
                <button>Add destination</button>
              </form>
            </section>
            <section className="list">
              {webhooks.map((w) => (
                <article key={w.id}>
                  <div>
                    <strong>{w.name}</strong>
                    <p>{w.url}</p>
                    {w.lastDeliveryError && (
                      <p className="warning">
                        Last delivery: {w.lastDeliveryError}
                      </p>
                    )}
                  </div>
                  <button
                    onClick={async () => {
                      const out = await request(`webhooks/${w.id}/`, {
                        method: "POST",
                        body: JSON.stringify({ action: "test" }),
                      });
                      setMessage(
                        out.delivered ? "Test delivery sent." : out.error,
                      );
                    }}
                  >
                    Send test
                  </button>
                </article>
              ))}
            </section>
          </>
        )}
        {view === "admin" && (
          <>
            <header>
              <p className="eyebrow">ADMIN CONTROL</p>
              <h1>Account approvals</h1>
              <p>Review registrations before they can create monitors.</p>
            </header>
            <section className="list">
              {users.map((person) => (
                <article key={person.id}>
                  <div>
                    <strong>{person.displayName || person.email}</strong>
                    <p>
                      {person.email} · {person.status}
                    </p>
                  </div>
                  {person.status === "pending" && (
                    <div className="actions">
                      <button onClick={() => updateUser(person.id, "approve")}>
                        Approve
                      </button>
                      <button
                        className="danger"
                        onClick={() => updateUser(person.id, "reject")}
                      >
                        Reject
                      </button>
                    </div>
                  )}
                </article>
              ))}
            </section>
          </>
        )}
      </section>
    </main>
  );
}

function App() {
  const [user, setUser] = useState(null),
    [ready, setReady] = useState(false);
  useEffect(() => {
    request("")
      .then((data) => {
        csrf = data.csrfToken;
        setUser(data.user);
        setReady(true);
      })
      .catch(() => setReady(true));
  }, []);
  if (!ready) return <div className="loading">Loading StockBot...</div>;
  return user ? (
    <Dashboard
      user={user}
      logout={async () => {
        await request("logout/", { method: "POST" });
        setUser(null);
      }}
    />
  ) : (
    <Auth onReady={setUser} />
  );
}
createRoot(document.getElementById("root")).render(<App />);
