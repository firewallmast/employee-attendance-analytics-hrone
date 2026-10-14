# Engineering Decisions

### 1. Indexes
I added a unique index on `emp_code` so the same employee cannot be added twice. I also used a unique compound index on `(emp_code, date)` to prevent multiple attendance records for the same employee on one day. Date indexes help with filtering and reports. I avoided adding too many indexes because they can slow down database writes.

### 2. Punch-in Race Condition
If two punch-in requests arrive at the same time for the same employee and date, both may try to create a record. The unique index allows only one to succeed. The other request gets a duplicate-key error, which the API returns as a `409 Conflict`.

### 3. Leaderboard Ties
I used competition ranking, so employees with the same total late minutes get the same rank, and the next rank is skipped. If several employees tie at the cutoff, all of them are included.

### 4. Headcount
The department summary starts with the employees collection and then looks up attendance records. This way, employees are counted even if they have no attendance logs for that month.

### 5. Scaling
If the data grew 100 times, I would first check query execution statistics to find slow operations. Then I would improve the necessary indexes and consider archiving older attendance records.