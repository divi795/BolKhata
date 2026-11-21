import pytest
from app.models import Customer, Transaction


def test_create_customer_success(client):
    """Test adding a customer with name and optional phone number."""
    payload = {"name": "Ramesh Kumar", "phone": "9876543210"}
    response = client.post("/customers", json=payload)
    assert response.status_code == 201
    data = response.json()
    assert data["id"] > 0
    assert data["name"] == "Ramesh Kumar"
    assert data["phone"] == "9876543210"
    assert data["current_balance"] == 0.0
    assert "created_at" in data


def test_create_customer_without_phone(client):
    """Test adding a customer without phone number (optional)."""
    payload = {"name": "Suresh Gupta"}
    response = client.post("/customers", json=payload)
    assert response.status_code == 201
    data = response.json()
    assert data["name"] == "Suresh Gupta"
    assert data["phone"] is None
    assert data["current_balance"] == 0.0


def test_create_customer_with_custom_id(client):
    """Test adding a customer with a specified unique customer ID."""
    payload = {"id": 101, "name": "Geeta Sharma", "phone": "9123456780"}
    response = client.post("/customers", json=payload)
    assert response.status_code == 201
    data = response.json()
    assert data["id"] == 101
    assert data["name"] == "Geeta Sharma"


def test_create_customer_duplicate_id_fails(client):
    """Test that creating a customer with an existing ID returns 409 Conflict."""
    payload = {"id": 200, "name": "First Customer"}
    res1 = client.post("/customers", json=payload)
    assert res1.status_code == 201

    payload_duplicate = {"id": 200, "name": "Duplicate ID Customer"}
    res2 = client.post("/customers", json=payload_duplicate)
    assert res2.status_code == 409
    assert "already exists" in res2.json()["detail"]


def test_create_customer_empty_or_whitespace_name(client):
    """Test validation fails if customer name is empty or only whitespace."""
    response = client.post("/customers", json={"name": "   "})
    assert response.status_code == 422


def test_view_all_customers_empty(client):
    """Test viewing all customers when database is empty."""
    response = client.get("/customers")
    assert response.status_code == 200
    assert response.json() == []


def test_view_all_customers_with_balances(client):
    """Test viewing all customers returns their current outstanding balances."""
    c1 = client.post("/customers", json={"name": "Amit Shah"}).json()
    c2 = client.post("/customers", json={"name": "Pooja Verma"}).json()

    # Add credit to Amit
    client.post(f"/customers/{c1['id']}/credit", json={"amount": 450.0, "description": "Atta & Oil"})

    # Fetch all
    response = client.get("/customers")
    assert response.status_code == 200
    customers = response.json()
    assert len(customers) == 2

    amit = next(c for c in customers if c["id"] == c1["id"])
    pooja = next(c for c in customers if c["id"] == c2["id"])
    assert amit["current_balance"] == 450.0
    assert pooja["current_balance"] == 0.0


def test_search_customers_by_name(client):
    """Test searching customers by name substring."""
    client.post("/customers", json={"name": "Rajesh Kumar"})
    client.post("/customers", json={"name": "Rakesh Sharma"})
    client.post("/customers", json={"name": "Mohan Lal"})

    # Search 'rajesh'
    res = client.get("/customers", params={"name": "rajesh"})
    assert res.status_code == 200
    data = res.json()
    assert len(data) == 1
    assert data[0]["name"] == "Rajesh Kumar"

    # Search 'rakesh' with 'search' parameter alias
    res2 = client.get("/customers", params={"search": "rakesh"})
    assert res2.status_code == 200
    assert len(res2.json()) == 1

    # Search 'kumar' substring
    res3 = client.get("/customers", params={"name": "kumar"})
    assert res3.status_code == 200
    assert len(res3.json()) == 1

    # Search non-matching
    res4 = client.get("/customers", params={"name": "Zubair"})
    assert res4.status_code == 200
    assert res4.json() == []


def test_get_customer_by_id(client):
    """Test retrieving an individual customer's details and balance."""
    c = client.post("/customers", json={"name": "Sunita Devi", "phone": "9998887776"}).json()
    client.post(f"/customers/{c['id']}/credit", json={"amount": 300.0})

    res = client.get(f"/customers/{c['id']}")
    assert res.status_code == 200
    data = res.json()
    assert data["id"] == c["id"]
    assert data["name"] == "Sunita Devi"
    assert data["phone"] == "9998887776"
    assert data["current_balance"] == 300.0


def test_get_nonexistent_customer(client):
    """Test 404 returned for non-existent customer ID."""
    res = client.get("/customers/99999")
    assert res.status_code == 404
    assert "not found" in res.json()["detail"].lower()


def test_invalid_customer_id_less_than_one(client):
    """Test error returned for invalid customer IDs <= 0."""
    res = client.get("/customers/0")
    assert res.status_code == 422


def test_add_credit_transaction(client):
    """Test adding a credit/udhaar transaction increases customer balance."""
    c = client.post("/customers", json={"name": "Dinesh Chandra"}).json()

    res = client.post(
        f"/customers/{c['id']}/credit",
        json={"amount": 600.0, "description": "Rice & Sugar"},
    )
    assert res.status_code == 201
    tx = res.json()
    assert tx["customer_id"] == c["id"]
    assert tx["type"] == "CREDIT"
    assert tx["amount"] == 600.0
    assert tx["description"] == "Rice & Sugar"

    # Verify balance
    c_updated = client.get(f"/customers/{c['id']}").json()
    assert c_updated["current_balance"] == 600.0


def test_add_payment_transaction(client):
    """Test adding a payment transaction decreases customer balance."""
    c = client.post("/customers", json={"name": "Anita Roy"}).json()

    # Credit ₹500
    client.post(f"/customers/{c['id']}/credit", json={"amount": 500.0, "description": "Udhaar"})
    # Payment ₹200
    res = client.post(
        f"/customers/{c['id']}/payment",
        json={"amount": 200.0, "description": "Cash payment via GPay"},
    )
    assert res.status_code == 201
    tx = res.json()
    assert tx["customer_id"] == c["id"]
    assert tx["type"] == "PAYMENT"
    assert tx["amount"] == 200.0

    # Balance should now be 300.0
    c_updated = client.get(f"/customers/{c['id']}").json()
    assert c_updated["current_balance"] == 300.0


def test_generic_transaction_endpoint(client):
    """Test adding transaction via /customers/{customer_id}/transactions."""
    c = client.post("/customers", json={"name": "Vikas Patel"}).json()

    # Generic credit
    res1 = client.post(
        f"/customers/{c['id']}/transactions",
        json={"type": "CREDIT", "amount": 1000.0, "description": "Wholesale soap"},
    )
    assert res1.status_code == 201
    assert res1.json()["type"] == "CREDIT"

    # Generic payment with colloquial Hindi 'jama'
    res2 = client.post(
        f"/customers/{c['id']}/transactions",
        json={"type": "jama", "amount": 400.0, "description": "Cash jama"},
    )
    assert res2.status_code == 201
    assert res2.json()["type"] == "PAYMENT"

    # Balance check
    c_updated = client.get(f"/customers/{c['id']}").json()
    assert c_updated["current_balance"] == 600.0


def test_amount_must_be_greater_than_zero(client):
    """Test that zero or negative transaction amounts are rejected."""
    c = client.post("/customers", json={"name": "Deepak Joshi"}).json()

    # Amount 0
    res_zero = client.post(
        f"/customers/{c['id']}/credit",
        json={"amount": 0.0, "description": "Zero amount"},
    )
    assert res_zero.status_code == 422

    # Negative amount
    res_neg = client.post(
        f"/customers/{c['id']}/payment",
        json={"amount": -50.0, "description": "Negative amount"},
    )
    assert res_neg.status_code == 422


def test_transaction_for_nonexistent_customer(client):
    """Test that adding transaction to nonexistent customer returns 404."""
    res = client.post(
        "/customers/99999/credit",
        json={"amount": 100.0, "description": "Udhaar"},
    )
    assert res.status_code == 404


def test_transaction_history_for_customer(client):
    """Test retrieving transaction history in descending order."""
    c = client.post("/customers", json={"name": "Kamla Devi"}).json()

    client.post(f"/customers/{c['id']}/credit", json={"amount": 250.0, "description": "Milk & Bread"})
    client.post(f"/customers/{c['id']}/credit", json={"amount": 150.0, "description": "Biscuits"})
    client.post(f"/customers/{c['id']}/payment", json={"amount": 300.0, "description": "Cash"})

    res = client.get(f"/customers/{c['id']}/transactions")
    assert res.status_code == 200
    txs = res.json()
    assert len(txs) == 3

    # Check newest first
    assert txs[0]["type"] == "PAYMENT"
    assert txs[0]["amount"] == 300.0
    assert txs[1]["type"] == "CREDIT"
    assert txs[1]["amount"] == 150.0
    assert txs[2]["type"] == "CREDIT"
    assert txs[2]["amount"] == 250.0


def test_customer_balance_breakdown_endpoint(client):
    """Test /customers/{customer_id}/balance endpoint."""
    c = client.post("/customers", json={"name": "Manoj Tiwari"}).json()

    client.post(f"/customers/{c['id']}/credit", json={"amount": 750.0})
    client.post(f"/customers/{c['id']}/payment", json={"amount": 250.0})

    res = client.get(f"/customers/{c['id']}/balance")
    assert res.status_code == 200
    data = res.json()
    assert data["customer_id"] == c["id"]
    assert data["customer_name"] == "Manoj Tiwari"
    assert data["total_credit"] == 750.0
    assert data["total_payment"] == 250.0
    assert data["current_balance"] == 500.0


def test_direct_transactions_router(client):
    """Test top-level /transactions routes."""
    c = client.post("/customers", json={"name": "Sanjay Kapoor"}).json()

    res_cr = client.post("/transactions/credit", json={"customer_id": c["id"], "amount": 1200.0, "description": "Refined oil"})
    assert res_cr.status_code == 201
    assert res_cr.json()["amount"] == 1200.0

    res_pm = client.post("/transactions/payment", json={"customer_id": c["id"], "amount": 500.0, "description": "PhonePe"})
    assert res_pm.status_code == 201
    assert res_pm.json()["amount"] == 500.0

    bal = client.get(f"/customers/{c['id']}/balance").json()
    assert bal["current_balance"] == 700.0


def test_advance_payment_and_multi_turn_balance(client):
    """Test ledger when payments exceed credit (advance payment) and subsequent credit restores balance."""
    c = client.post("/customers", json={"name": "Prakash Jha"}).json()

    # Customer pays ₹500 advance without prior credit
    client.post(f"/customers/{c['id']}/payment", json={"amount": 500.0, "description": "Advance token"})
    bal1 = client.get(f"/customers/{c['id']}/balance").json()
    assert bal1["current_balance"] == -500.0  # Shopkeeper owes goods/change

    # Customer later buys ₹300 worth of goods
    client.post(f"/customers/{c['id']}/credit", json={"amount": 300.0, "description": "Wheat flour"})
    bal2 = client.get(f"/customers/{c['id']}/balance").json()
    assert bal2["current_balance"] == -200.0

    # Customer buys ₹400 more goods
    client.post(f"/customers/{c['id']}/credit", json={"amount": 400.0, "description": "Ghee"})
    bal3 = client.get(f"/customers/{c['id']}/balance").json()
    assert bal3["current_balance"] == 200.0  # Now customer owes ₹200


def test_database_persistence(client, db_session):
    """Test that customer and transaction records are persistently committed to the database."""
    c_res = client.post("/customers", json={"name": "DB Persistent Customer", "phone": "9991112233"}).json()
    client.post(f"/customers/{c_res['id']}/credit", json={"amount": 800.0, "description": "Persistent Credit"})

    # Query directly via SQLAlchemy session
    customer_db = db_session.query(Customer).filter(Customer.id == c_res["id"]).first()
    assert customer_db is not None
    assert customer_db.name == "DB Persistent Customer"
    assert customer_db.phone == "9991112233"

    transactions_db = db_session.query(Transaction).filter(Transaction.customer_id == c_res["id"]).all()
    assert len(transactions_db) == 1
    assert transactions_db[0].amount == 800.0
    assert transactions_db[0].type == "CREDIT"
