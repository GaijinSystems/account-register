// Written by tools/build_demo.py from the app's Python modules (version 1.0.0). Not edited by hand.
"use strict";
window.REGISTER_DEMO_SPEC = {
 "version": "1.0.0",
 "routes": [
  {
   "method": "GET",
   "path": "^(?:/api/status)$",
   "op": "status",
   "allowed": [],
   "required": []
  },
  {
   "method": "GET",
   "path": "^(?:/api/tabs)$",
   "op": "list_tabs",
   "allowed": [],
   "required": []
  },
  {
   "method": "GET",
   "path": "^(?:/api/suggestions)$",
   "op": "suggestions",
   "allowed": [],
   "required": []
  },
  {
   "method": "POST",
   "path": "^(?:/api/accounts)$",
   "op": "add_account",
   "allowed": [
    "name",
    "opening_balance",
    "owed_positive",
    "type",
    "year"
   ],
   "required": [
    "name",
    "opening_balance",
    "type"
   ]
  },
  {
   "method": "PATCH",
   "path": "^(?:/api/accounts/(?<account>[a-z0-9-]{1,40}))$",
   "op": "rename_account",
   "allowed": [
    "name",
    "owed_positive"
   ],
   "required": [
    "name"
   ]
  },
  {
   "method": "POST",
   "path": "^(?:/api/accounts/(?<account>[a-z0-9-]{1,40})/years)$",
   "op": "add_year",
   "allowed": [],
   "required": []
  },
  {
   "method": "GET",
   "path": "^(?:/api/sheet/(?<account>[a-z0-9-]{1,40})/(?<year>\\d{4}|-))$",
   "op": "get_sheet",
   "allowed": [],
   "required": []
  },
  {
   "method": "POST",
   "path": "^(?:/api/sheet/(?<account>[a-z0-9-]{1,40})/(?<year>\\d{4}|-)/rows)$",
   "op": "insert_row",
   "allowed": [
    "fields",
    "index",
    "rev"
   ],
   "required": [
    "index",
    "rev"
   ]
  },
  {
   "method": "PUT",
   "path": "^(?:/api/sheet/(?<account>[a-z0-9-]{1,40})/(?<year>\\d{4}|-)/rows/(?<index>\\d{1,6}))$",
   "op": "update_row",
   "allowed": [
    "fields",
    "rev"
   ],
   "required": [
    "fields",
    "rev"
   ]
  },
  {
   "method": "DELETE",
   "path": "^(?:/api/sheet/(?<account>[a-z0-9-]{1,40})/(?<year>\\d{4}|-)/rows/(?<index>\\d{1,6}))$",
   "op": "delete_row",
   "allowed": [
    "rev"
   ],
   "required": [
    "rev"
   ]
  },
  {
   "method": "GET",
   "path": "^(?:/api/sheet/(?<account>[a-z0-9-]{1,40})/(?<year>\\d{4}|-)/history)$",
   "op": "history",
   "allowed": [
    "limit"
   ],
   "required": []
  },
  {
   "method": "POST",
   "path": "^(?:/api/sheet/(?<account>[a-z0-9-]{1,40})/(?<year>\\d{4}|-)/undo)$",
   "op": "undo",
   "allowed": [
    "commit",
    "rev"
   ],
   "required": [
    "commit",
    "rev"
   ]
  },
  {
   "method": "POST",
   "path": "^(?:/api/sheet/(?<account>[a-z0-9-]{1,40})/(?<year>\\d{4}|-)/reconcile)$",
   "op": "reconcile",
   "allowed": [
    "cleared",
    "rev",
    "uncleared"
   ],
   "required": [
    "cleared",
    "rev"
   ]
  },
  {
   "method": "GET",
   "path": "^(?:/api/recurring)$",
   "op": "list_recurring",
   "allowed": [],
   "required": []
  },
  {
   "method": "POST",
   "path": "^(?:/api/recurring)$",
   "op": "add_recurring",
   "allowed": [
    "account",
    "amount",
    "category",
    "description",
    "every",
    "note",
    "start"
   ],
   "required": [
    "account",
    "amount",
    "description",
    "every",
    "start"
   ]
  },
  {
   "method": "POST",
   "path": "^(?:/api/recurring/enter)$",
   "op": "enter_recurring",
   "allowed": [
    "ids"
   ],
   "required": []
  },
  {
   "method": "POST",
   "path": "^(?:/api/recurring/(?<id>[0-9a-f]{16})/skip)$",
   "op": "skip_recurring",
   "allowed": [
    "count"
   ],
   "required": []
  },
  {
   "method": "DELETE",
   "path": "^(?:/api/recurring/(?<id>[0-9a-f]{16}))$",
   "op": "delete_recurring",
   "allowed": [],
   "required": []
  },
  {
   "method": "POST",
   "path": "^(?:/api/report)$",
   "op": "report",
   "allowed": [
    "account",
    "year"
   ],
   "required": []
  },
  {
   "method": "POST",
   "path": "^(?:/api/search)$",
   "op": "search",
   "allowed": [
    "account",
    "amount_max",
    "amount_min",
    "date_from",
    "date_to",
    "text"
   ],
   "required": []
  },
  {
   "method": "POST",
   "path": "^(?:/api/transfers)$",
   "op": "add_transfer",
   "allowed": [
    "amount",
    "category",
    "date",
    "description",
    "from_account",
    "id",
    "note",
    "to_account"
   ],
   "required": [
    "amount",
    "date",
    "from_account",
    "to_account"
   ]
  },
  {
   "method": "POST",
   "path": "^(?:/api/export)$",
   "op": "export",
   "allowed": [
    "password"
   ],
   "required": [
    "password"
   ]
  },
  {
   "method": "POST",
   "path": "^(?:/api/password)$",
   "op": "change_password",
   "allowed": [
    "new_password",
    "password"
   ],
   "required": [
    "new_password",
    "password"
   ]
  }
 ],
 "ops": {
  "status": {},
  "unlock": {
   "password": [
    "str",
    true
   ]
  },
  "lock": {},
  "list_tabs": {},
  "search": {
   "account": [
    "str",
    false
   ],
   "text": [
    "str",
    false
   ],
   "amount_min": [
    "str",
    false
   ],
   "amount_max": [
    "str",
    false
   ],
   "date_from": [
    "str",
    false
   ],
   "date_to": [
    "str",
    false
   ]
  },
  "report": {
   "year": [
    "int",
    false
   ],
   "account": [
    "str",
    false
   ]
  },
  "suggestions": {},
  "get_sheet": {
   "account": [
    "str",
    true
   ],
   "year": [
    "year",
    true
   ]
  },
  "insert_row": {
   "account": [
    "str",
    true
   ],
   "year": [
    "year",
    true
   ],
   "rev": [
    "str",
    true
   ],
   "index": [
    "int",
    true
   ],
   "fields": [
    "dict",
    false
   ]
  },
  "update_row": {
   "account": [
    "str",
    true
   ],
   "year": [
    "year",
    true
   ],
   "rev": [
    "str",
    true
   ],
   "index": [
    "int",
    true
   ],
   "fields": [
    "dict",
    true
   ]
  },
  "delete_row": {
   "account": [
    "str",
    true
   ],
   "year": [
    "year",
    true
   ],
   "rev": [
    "str",
    true
   ],
   "index": [
    "int",
    true
   ]
  },
  "history": {
   "account": [
    "str",
    true
   ],
   "year": [
    "year",
    true
   ],
   "limit": [
    "int",
    false
   ]
  },
  "undo": {
   "account": [
    "str",
    true
   ],
   "year": [
    "year",
    true
   ],
   "rev": [
    "str",
    true
   ],
   "commit": [
    "str",
    true
   ]
  },
  "reconcile": {
   "account": [
    "str",
    true
   ],
   "year": [
    "year",
    true
   ],
   "rev": [
    "str",
    true
   ],
   "cleared": [
    "list",
    true
   ],
   "uncleared": [
    "list",
    false
   ]
  },
  "add_transfer": {
   "from_account": [
    "str",
    true
   ],
   "to_account": [
    "str",
    true
   ],
   "date": [
    "str",
    true
   ],
   "amount": [
    "str",
    true
   ],
   "description": [
    "str",
    false
   ],
   "category": [
    "str",
    false
   ],
   "note": [
    "str",
    false
   ],
   "id": [
    "str",
    false
   ]
  },
  "list_recurring": {},
  "add_recurring": {
   "account": [
    "str",
    true
   ],
   "description": [
    "str",
    true
   ],
   "amount": [
    "str",
    true
   ],
   "every": [
    "str",
    true
   ],
   "start": [
    "str",
    true
   ],
   "category": [
    "str",
    false
   ],
   "note": [
    "str",
    false
   ]
  },
  "delete_recurring": {
   "id": [
    "str",
    true
   ]
  },
  "skip_recurring": {
   "id": [
    "str",
    true
   ],
   "count": [
    "int",
    false
   ]
  },
  "enter_recurring": {
   "ids": [
    "list",
    false
   ]
  },
  "add_year": {
   "account": [
    "str",
    true
   ]
  },
  "add_account": {
   "name": [
    "str",
    true
   ],
   "type": [
    "str",
    true
   ],
   "opening_balance": [
    "str",
    true
   ],
   "year": [
    "year",
    false
   ],
   "owed_positive": [
    "bool",
    false
   ]
  },
  "rename_account": {
   "account": [
    "str",
    true
   ],
   "name": [
    "str",
    true
   ],
   "owed_positive": [
    "bool",
    false
   ]
  },
  "export": {
   "password": [
    "str",
    true
   ]
  },
  "change_password": {
   "password": [
    "str",
    true
   ],
   "new_password": [
    "str",
    true
   ]
  }
 },
 "error_status": {
  "stale": 409,
  "repo_dirty": 423,
  "not_found": 404,
  "conflict": 422,
  "bad_request": 400,
  "invalid": 400,
  "unknown_field": 400,
  "balance_readonly": 400,
  "too_large": 413,
  "locked": 401,
  "throttled": 429,
  "bad_password": 403
 },
 "fields": [
  "num",
  "date",
  "description",
  "category",
  "amount",
  "cleared",
  "note",
  "transfer",
  "bank_id"
 ],
 "link_fields": [
  "transfer",
  "bank_id"
 ],
 "search_fields": [
  "description",
  "category",
  "note",
  "num"
 ],
 "text_limits": {
  "num": 20,
  "description": 200,
  "category": 60,
  "note": 500,
  "bank_id": 255
 },
 "name_limit": 60,
 "min_year": 1900,
 "max_year": 2199,
 "max_abs": "999999999999.99",
 "transfer_category": "Transfer",
 "opening": "Opening balance",
 "account_id": "^(?:[a-z0-9](?:[a-z0-9-]{0,38}[a-z0-9])?)$",
 "account_id_limit": 40,
 "amount": "^(?:-?(0|[1-9][0-9]{0,11})\\.[0-9]{2})$",
 "iso_date": "^(?:[0-9]{4}-[0-9]{2}-[0-9]{2})$",
 "forbidden_categories": [
  "Cc",
  "Cf",
  "Cn",
  "Co",
  "Cs"
 ],
 "accounts_file": "accounts.json",
 "recurring_file": "recurring.json",
 "link_id": "^(?:[0-9a-f]{16})$",
 "types": [
  {
   "id": "checking",
   "label": "Checking",
   "yearly": true,
   "owed_positive": false,
   "debit_mode": true
  },
  {
   "id": "savings",
   "label": "Savings",
   "yearly": false,
   "owed_positive": false,
   "debit_mode": false
  },
  {
   "id": "card",
   "label": "Credit card",
   "yearly": true,
   "owed_positive": true,
   "debit_mode": true
  },
  {
   "id": "cash",
   "label": "Cash",
   "yearly": false,
   "owed_positive": false,
   "debit_mode": false
  },
  {
   "id": "loan",
   "label": "Loan",
   "yearly": false,
   "owed_positive": true,
   "debit_mode": false
  }
 ],
 "recurring_every": {
  "week": [
   "days",
   7
  ],
  "2weeks": [
   "days",
   14
  ],
  "month": [
   "months",
   1
  ],
  "quarter": [
   "months",
   3
  ],
  "year": [
   "months",
   12
  ]
 },
 "recurring_max_templates": 200,
 "recurring_max_due": 60,
 "max_suggestions": 1000,
 "max_results": 500,
 "max_report_rows": 200,
 "history_default": 30,
 "history_max": 60,
 "history_changes": 40
};
