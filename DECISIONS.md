# Design Decisions

## 1. Indexes

I created a unique index on `employees.emp_code` to prevent duplicate employees and a unique compound index on `attendance_logs.emp_code` and `date` to guarantee one attendance record per employee per day. I also added indexes for department queries, attendance date ranges, and employee/date/status filtering. I would consider adding a separate index on `joined_on` if query analysis shows that headcount queries without a department filter need optimization.

## 2. Punch-in Race Condition

Two identical requests can both check whether an employee exists. Both may attempt to insert an attendance record, but the unique employee/date index allows only one insert. The successful request returns HTTP 201, and the duplicate request returns HTTP 409 Conflict.

## 3. Leaderboard Ties

The leaderboard uses MongoDB's `$rank` to rank employees by total late minutes. Employees tied at the cutoff rank are included, so the response may contain more entries than the requested limit. This ensures that employees with the same rank are treated equally.

## 4. Headcount

The department summary starts with eligible employee documents and uses `$lookup` to find their attendance records. The aggregation preserves employees without attendance logs, ensuring that they are still included in the department headcount.

## 5. Scaling to 100 Times More Data

If the dataset grew by 100 times, I would benchmark representative queries, inspect MongoDB execution plans, and add or adjust indexes based on measured bottlenecks. I would also test analytics performance using a production-sized dataset and optimize aggregation pipelines where necessary.