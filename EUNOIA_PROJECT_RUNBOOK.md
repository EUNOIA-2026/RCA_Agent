# Eunoia DevOps RCA Project — Complete Runbook

> **Purpose:** Single copy/paste reference for running, testing, building, pushing, and deploying the Student Record Manager and the separate Eunoia RCA Agent.
>
> **Important:** Never put AWS access keys, secret keys, session tokens, Azure API keys, or other credentials in this file or in Git.

---

## 1. Project layout

```text
D:\RTB_Agents\
│
├── .venv\
├── APP_EKS\                 # Application project
└── RCA_Agent\               # Separate RCA/DevOps agent
```

### Application

```text
D:\RTB_Agents\APP_EKS\
│
├── backend\
│   ├── app.py
│   ├── requirements.txt
│   └── data\
│       └── students.csv
│
├── frontend\                # Angular source
│   ├── src\
│   ├── public\
│   ├── package.json
│   ├── package-lock.json
│   └── angular.json
│
├── build\
│   └── frontend\            # Angular production build used by Docker
│
└── Dockerfile
```

### RCA Agent

```text
D:\RTB_Agents\RCA_Agent\
│
├── main.py
├── main.py.backup
├── .env                      # Secrets/configuration — DO NOT commit
└── reports\
    ├── frontend\
    ├── backend\
    └── database\
```

---

# 2. Fixed project parameters

These are the AWS/application values already used by this project.

```text
AWS Account ID:              583164063998
AWS Region:                  us-east-1

ECR Repository:              simple-python-app
ECR Registry:                583164063998.dkr.ecr.us-east-1.amazonaws.com

EKS Deployment:              simple-python-app
Kubernetes namespace:        default
Application container:       simple-python-app
Application port:            5000
Local application URL:       http://127.0.0.1:5000

CloudWatch Log Group:
/aws/containerinsights/Eunoia/application

Application project:
D:\RTB_Agents\APP_EKS

RCA Agent project:
D:\RTB_Agents\RCA_Agent

Azure Agent:
Agent-Eunoia
```

### Current design

```text
Angular Frontend
      ↓
Python Flask Backend
      ↓
students.csv
      ↓
Docker
      ↓
ECR
      ↓
EKS
      ↓
Fluent Bit
      ↓
CloudWatch Logs
      ↓
RCA Agent
      ↓
Azure Agent-Eunoia
      ↓
RCA Report
```

The application and RCA Agent are **separate processes**.

---

# 3. AWS CLI — verify current identity

Run in VS Code PowerShell terminal or normal PowerShell.

```powershell
# Show the AWS identity currently used by the CLI.
# This does NOT print your secret key.
aws sts get-caller-identity

# Confirm the configured AWS region.
aws configure get region

# Show how the AWS CLI is currently sourcing credentials.
aws configure list
```

Expected account ID:

```text
583164063998
```

Do **not** paste access keys or secret keys into this runbook.

---

# 4. AWS CLI configuration (only when needed)

If the machine is not authenticated:

```powershell
# Configure the AWS CLI interactively.
# Do NOT store the entered secret values in this file.
aws configure
```

Use:

```text
AWS Access Key ID:       <your AWS access key>
AWS Secret Access Key:   <your AWS secret key>
Default region name:     us-east-1
Default output format:   json
```

Then verify:

```powershell
aws sts get-caller-identity
```

---

# 5. EKS configuration

If kubectl is not currently connected to the EKS cluster, first discover the cluster name:

```powershell
# List EKS clusters in the project region.
aws eks list-clusters --region us-east-1
```

Then update kubeconfig using the actual cluster name returned above:

```powershell
# Replace YOUR_CLUSTER_NAME with the real EKS cluster name.
aws eks update-kubeconfig --region us-east-1 --name YOUR_CLUSTER_NAME
```

Verify connectivity:

```powershell
# Show EKS worker nodes.
kubectl get nodes

# Show the existing application deployment.
kubectl get deployment simple-python-app

# Show application pods.
kubectl get pods -o wide
```

---

# 6. Tool/version checks

```powershell
# Python
python --version

# Node.js
node --version

# npm
npm --version

# Docker
 docker --version

# Kubernetes client
kubectl version --client

# AWS CLI
aws --version
```

---

# 7. Start the APPLICATION separately

## Window 1 — EKS application access

Use a separate VS Code terminal:

```powershell
# Go to the application project.
cd D:\RTB_Agents\APP_EKS

# Forward local port 5000 to the EKS application.
kubectl port-forward deployment/simple-python-app 5000:5000
```

Expected:

```text
Forwarding from 127.0.0.1:5000 -> 5000
Forwarding from [::1]:5000 -> 5000
```

Keep this terminal running.

Open in browser:

```text
http://127.0.0.1:5000
```

---

# 8. Start the EUNOIA RCA AGENT separately

## Window 2 — RCA Agent

```powershell
# Go to the separate RCA Agent project.
cd D:\RTB_Agents\RCA_Agent

# Start continuous CloudWatch monitoring and automatic Eunoia invocation.
python .\main.py
```

Keep this terminal running.

The intended behavior is:

```text
NEW runtime ERROR in CloudWatch
        ↓
RCA Agent detects it automatically
        ↓
Agent-Eunoia invoked
        ↓
RCA report generated
```

---

# 9. Verify RCA Agent environment WITHOUT exposing secrets

Run:

```powershell
# Check whether the .env file exists.
Test-Path .\.env
```

Then safely inspect only whether required values are present:

```powershell
# Print status of important variables without printing secret values.
python -c "from pathlib import Path; from dotenv import dotenv_values; v=dotenv_values(Path('.env')); print('AZURE_AI_PROJECT_ENDPOINT=', '<SET>' if v.get('AZURE_AI_PROJECT_ENDPOINT') else '<MISSING>'); print('AGENT_NAME=', v.get('AGENT_NAME')); print('API_KEY=', '<SET>' if v.get('API_KEY') else '<MISSING>'); print('API_VERSION=', v.get('API_VERSION')); print('REPORT_DIR=', v.get('REPORT_DIR')); print('FALLBACK_POLL_INTERVAL=', v.get('FALLBACK_POLL_INTERVAL'))"
```

Known agent name:

```text
Agent-Eunoia
```

The `.env` must remain only in the RCA Agent directory and must not be committed.

---

# 10. Application normal-operation test

Open:

```text
http://127.0.0.1:5000
```

Normal Student Record Manager operations:

```text
1. Add Student
2. View / Refresh Students
3. Search Student
4. Delete Student
5. View Class Summary
```

The normal application must work before testing failures.

---

# 11. Build Angular frontend

From the application directory:

```powershell
# Go to Angular project.
cd D:\RTB_Agents\APP_EKS\frontend

# Install frontend packages when needed.
npm install

# Create production Angular build.
npm run build
```

The build is expected under:

```text
D:\RTB_Agents\APP_EKS\frontend\dist\frontend\browser
```

---

# 12. Copy Angular build into Docker build folder

```powershell
# Return to application root.
cd D:\RTB_Agents\APP_EKS

# Remove the old packaged frontend build.
Remove-Item .\build\frontend -Recurse -Force -ErrorAction SilentlyContinue

# Recreate the directory.
New-Item -ItemType Directory -Force .\build\frontend | Out-Null

# Copy the newest Angular production files.
Copy-Item .\frontend\dist\frontend\browser\* .\build\frontend -Recurse -Force

# Verify the packaged frontend.
Get-ChildItem .\build\frontend
```

Expected files include:

```text
index.html
main-*.js
styles-*.css
favicon.ico
```

---

# 13. Dockerfile used by this project

The Dockerfile is at:

```text
D:\RTB_Agents\APP_EKS\Dockerfile
```

Expected structure:

```dockerfile
FROM python:3.12-slim

WORKDIR /app

COPY backend/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app.py .
COPY backend/data ./backend/data
COPY build/frontend ./frontend-dist

EXPOSE 5000

CMD ["python", "app.py"]
```

Important: the root `app.py` is the application version that was tested locally.

---

# 14. Local backend test

From application root:

```powershell
cd D:\RTB_Agents\APP_EKS

# Start the application locally.
python .\app.py
```

Open:

```text
http://127.0.0.1:5000
```

Stop it with:

```text
Ctrl + C
```

---

# 15. Build Docker image

Always use a unique version tag.

Example:

```powershell
cd D:\RTB_Agents\APP_EKS

# Build a new version of the Student Record Manager.
docker build --no-cache -t simple-python-app:student-manager-v2 .
```

Verify:

```powershell
docker images simple-python-app
```

For future releases, use:

```text
student-manager-v3
student-manager-v4
student-manager-v5
```

instead of repeatedly overwriting `latest`.

---

# 16. Authenticate Docker to ECR

ECR authorization tokens expire.

Run before a push:

```powershell
# Authenticate Docker to the existing ECR registry.
aws ecr get-login-password --region us-east-1 | docker login --username AWS --password-stdin 583164063998.dkr.ecr.us-east-1.amazonaws.com
```

Expected:

```text
Login Succeeded
```

---

# 17. Tag Docker image for ECR

```powershell
# Tag the local image for the existing ECR repository.
docker tag simple-python-app:student-manager-v2 583164063998.dkr.ecr.us-east-1.amazonaws.com/simple-python-app:student-manager-v2
```

---

# 18. Push Docker image to existing ECR

```powershell
# Push the application image to the existing ECR repository.
docker push 583164063998.dkr.ecr.us-east-1.amazonaws.com/simple-python-app:student-manager-v2
```

Wait for:

```text
student-manager-v2: digest: sha256:...
```

Do not update EKS until the push succeeds.

---

# 19. Verify image in ECR

```powershell
# Confirm the new image tag exists in ECR.
aws ecr describe-images `
  --repository-name simple-python-app `
  --region us-east-1 `
  --query "imageDetails[?imageTags && contains(imageTags, 'student-manager-v2')].[imageTags,imageDigest]" `
  --output table
```

---

# 20. Update EXISTING EKS deployment

Do not create a second deployment.

```powershell
# Update the existing application deployment to the new ECR tag.
kubectl set image deployment/simple-python-app `
  simple-python-app=583164063998.dkr.ecr.us-east-1.amazonaws.com/simple-python-app:student-manager-v2
```

Verify the deployment image:

```powershell
# Show the image configured in the deployment.
kubectl get deployment simple-python-app `
  -o jsonpath="{.spec.template.spec.containers[0].image}"
```

Expected:

```text
583164063998.dkr.ecr.us-east-1.amazonaws.com/simple-python-app:student-manager-v2
```

---

# 21. EKS low-capacity clean replacement procedure

This cluster has previously reported:

```text
0/2 nodes are available: 2 Too many pods
```

When a rolling update gets stuck with the new pod in `Pending`, use the clean one-replica replacement procedure below.

First inspect:

```powershell
kubectl get pods -o wide

# If a new pod is Pending, inspect the scheduling reason.
kubectl describe pod PENDING_POD_NAME
```

If the problem is `Too many pods`, use:

```powershell
# Stop the single application replica temporarily.
kubectl scale deployment/simple-python-app --replicas=0

# Confirm the application pod has terminated.
kubectl get pods
```

Then ensure the deployment uses the desired image:

```powershell
kubectl set image deployment/simple-python-app `
  simple-python-app=583164063998.dkr.ecr.us-east-1.amazonaws.com/simple-python-app:student-manager-v2
```

Start one fresh replica:

```powershell
# Start one fresh application pod.
kubectl scale deployment/simple-python-app --replicas=1
```

Check:

```powershell
kubectl get pods -o wide
```

Expected:

```text
simple-python-app-...   1/1   Running
```

Verify image:

```powershell
kubectl get pod -l app=simple-python-app `
  -o jsonpath="{.items[0].spec.containers[0].image}"
```

---

# 22. EKS rollout status

```powershell
# Watch the deployment rollout.
kubectl rollout status deployment/simple-python-app
```

If it hangs with `old replicas pending termination`, inspect:

```powershell
kubectl get pods -o wide
kubectl get events --sort-by=.lastTimestamp
```

Do not blindly delete pods until the status is understood.

---

# 23. Verify application logs in EKS

```powershell
# View recent application logs.
kubectl logs deployment/simple-python-app --tail=100
```

Follow logs live:

```powershell
kubectl logs deployment/simple-python-app -f
```

Stop following with:

```text
Ctrl + C
```

---

# 24. Port-forward EKS application

## Window 1

```powershell
cd D:\RTB_Agents\APP_EKS

# Expose EKS service locally for the browser.
kubectl port-forward deployment/simple-python-app 5000:5000
```

Expected:

```text
Forwarding from 127.0.0.1:5000 -> 5000
```

Browser:

```text
http://127.0.0.1:5000
```

Keep the port-forward terminal running.

---

# 25. Check local port 5000 if port-forward fails

```powershell
# Check whether another process is using local port 5000.
netstat -ano | findstr :5000
```

If an old process is holding the port, get its PID from the output and stop only that process:

```powershell
# Replace PID_NUMBER with the process ID returned above.
taskkill /PID PID_NUMBER /F
```

Then restart:

```powershell
kubectl port-forward deployment/simple-python-app 5000:5000
```

---

# 26. CloudWatch checks

CloudWatch application log group:

```text
/aws/containerinsights/Eunoia/application
```

List recent application streams:

```powershell
aws logs describe-log-streams `
  --region us-east-1 `
  --log-group-name "/aws/containerinsights/Eunoia/application" `
  --order-by LastEventTime `
  --descending `
  --max-items 10
```

Filter streams containing the application name:

```powershell
aws logs describe-log-streams `
  --region us-east-1 `
  --log-group-name "/aws/containerinsights/Eunoia/application" `
  --order-by LastEventTime `
  --descending `
  --query "logStreams[?contains(logStreamName, 'simple-python-app')].[logStreamName,lastEventTimestamp]" `
  --output table
```

Important: **Do not hard-code the EKS pod name in the RCA watcher.**

EKS pod/log stream names can change after deployment or restart.

---

# 27. Read a specific CloudWatch stream manually

First discover the stream:

```powershell
aws logs describe-log-streams `
  --region us-east-1 `
  --log-group-name "/aws/containerinsights/Eunoia/application" `
  --log-stream-name-prefix "ip-172-31-20-21.ec2.internal-application.var.log.containers.simple-python-app" `
  --max-items 10
```

Then retrieve events using the returned `logStreamName`:

```powershell
# Replace STREAM_NAME with the exact returned stream name.
aws logs get-log-events `
  --region us-east-1 `
  --log-group-name "/aws/containerinsights/Eunoia/application" `
  --log-stream-name "STREAM_NAME" `
  --limit 50
```

---

# 28. RCA Agent startup — final demo procedure

## Window 1 — Application access

```powershell
cd D:\RTB_Agents\APP_EKS
kubectl port-forward deployment/simple-python-app 5000:5000
```

## Window 2 — Eunoia RCA Agent

```powershell
cd D:\RTB_Agents\RCA_Agent
python .\main.py
```

## Browser

```text
http://127.0.0.1:5000
```

The RCA Agent must remain running while the application is being tested.

---

# 29. Normal demo flow

Use the browser and demonstrate:

```text
Student Record Manager
        ↓
Add Student
        ↓
View Students
        ↓
Search Student
        ↓
Delete Student
        ↓
Class Summary
```

There should be **no visible Generate Error buttons**.

---

# 30. Dynamic FRONTEND error demo

This is a runtime failure, not a pre-fed RCA event.

1. Open Search Student.
2. Enter an ID that does not exist, for example:

```text
9999
```

3. Click Search.

The intentional frontend defect should cause a real JavaScript error such as:

```text
TypeError: Cannot read properties of null (reading 'name')
```

The Angular code reports the real message and stack to:

```text
POST /api/error/frontend
```

Python then logs:

```text
FRONTEND_ERROR: ...
```

Expected RCA location:

```text
D:\RTB_Agents\RCA_Agent\reports\frontend\
```

---

# 31. Dynamic BACKEND error demo

1. Delete all student records.
2. Click View Class Summary.
3. With zero students, the current demonstration defect performs:

```python
average_age = total_age / len(students)
```

4. This produces a real runtime exception:

```text
ZeroDivisionError: division by zero
```

Python uses `logging.exception()`, so the traceback is logged.

Expected RCA location:

```text
D:\RTB_Agents\RCA_Agent\reports\backend\
```

---

# 32. Dynamic DATABASE / CSV error demo

For a controlled database-layer demonstration:

Stop the local app if running locally:

```text
Ctrl + C
```

Make a backup:

```powershell
cd D:\RTB_Agents\APP_EKS
Copy-Item .\backend\data\students.csv .\backend\data\students.backup.csv -Force
```

Simulate a missing database file:

```powershell
Rename-Item .\backend\data\students.csv students.missing.csv
```

Start the application again or test the already deployed version with the equivalent controlled database failure.

When the app tries to load student data, the backend should log a real file/database error.

Expected RCA location:

```text
D:\RTB_Agents\RCA_Agent\reports\database\
```

Restore the CSV after the test:

```powershell
Rename-Item .\backend\data\students.missing.csv students.csv
```

---

# 33. Important: local errors vs EKS RCA

A local Flask run proves that the application creates the intended runtime exception.

It does **not** automatically invoke Eunoia unless that log enters the CloudWatch/EKS pipeline.

For the actual jury demo, use:

```text
Browser
  ↓
EKS application
  ↓
stdout/stderr
  ↓
Fluent Bit
  ↓
CloudWatch
  ↓
RCA Agent
  ↓
Agent-Eunoia
  ↓
RCA report
```

---

# 34. Verify a runtime error reached EKS

For a backend summary error, after all students are removed:

```powershell
# Trigger the endpoint through the EKS port-forward.
curl.exe -i http://127.0.0.1:5000/api/summary
```

Then:

```powershell
# Verify the application container emitted the runtime traceback.
kubectl logs deployment/simple-python-app --tail=100
```

Look for:

```text
BACKEND_ERROR: Student summary calculation failed
ZeroDivisionError: division by zero
```

---

# 35. Check generated reports

```powershell
# Frontend RCA reports
Get-ChildItem D:\RTB_Agents\RCA_Agent\reports\frontend

# Backend RCA reports
Get-ChildItem D:\RTB_Agents\RCA_Agent\reports\backend

# Database RCA reports
Get-ChildItem D:\RTB_Agents\RCA_Agent\reports\database
```

Read the newest backend report:

```powershell
Get-Content D:\RTB_Agents\RCA_Agent\reports\backend\rca-report-*.md
```

---

# 36. Expected automatic RCA classification

The watcher should classify explicit markers first:

```text
FRONTEND_ERROR  → reports/frontend/
BACKEND_ERROR   → reports/backend/
DATABASE_ERROR  → reports/database/
```

Do not classify only on the generic word `ERROR`.

The watcher should use the actual new CloudWatch event, not a pre-written error feed.

---

# 37. RCA Agent reliability requirements

The RCA Agent should:

```text
✓ Poll continuously
✓ Detect NEW CloudWatch ERROR events
✓ Discover changing EKS log streams dynamically
✓ Survive EKS pod replacement
✓ Avoid hard-coded pod names
✓ Capture traceback/context
✓ Classify frontend/backend/database
✓ Invoke Agent-Eunoia automatically
✓ Write reports into the correct directory
✓ Avoid duplicate processing of the same CloudWatch event
```

The application must NOT launch the RCA Agent.

The RCA Agent must NOT launch the application.

---

# 38. Useful Kubernetes commands

```powershell
# Current pods
kubectl get pods -o wide

# Deployment
kubectl get deployment simple-python-app

# Deployment YAML
kubectl get deployment simple-python-app -o yaml

# Current image
kubectl get deployment simple-python-app `
  -o jsonpath="{.spec.template.spec.containers[0].image}"

# Service list
kubectl get svc

# Recent Kubernetes events
kubectl get events --sort-by=.lastTimestamp

# Detailed pod diagnostics
kubectl describe pod POD_NAME

# Application logs
kubectl logs deployment/simple-python-app --tail=100
```

---

# 39. Emergency rollback

If the newly deployed application is bad:

```powershell
# Show rollout history.
kubectl rollout history deployment/simple-python-app

# Roll back to the previous deployment revision.
kubectl rollout undo deployment/simple-python-app

# Verify pod status.
kubectl get pods -o wide

# Verify rollout.
kubectl rollout status deployment/simple-python-app
```

---

# 40. Suggested Git ignore rules

Make sure your `.gitignore` includes at least:

```gitignore
# Python
__pycache__/
*.pyc
.venv/

# Environment secrets
.env
*.env

# Angular
frontend/node_modules/
frontend/.angular/
frontend/dist/

# Generated build output
build/frontend/

# Generated RCA reports
RCA_Agent/reports/
```

If you intentionally need generated reports in source control, remove the final report ignore line.

Never commit credentials.

---

# 41. Full deployment sequence — copy/paste order

Use this sequence for a new application release.

```powershell
# ============================================
# A. APPLICATION
# ============================================
cd D:\RTB_Agents\APP_EKS

# Build Angular
cd .\frontend
npm install
npm run build

# Package Angular build
cd ..
Remove-Item .\build\frontend -Recurse -Force -ErrorAction SilentlyContinue
New-Item -ItemType Directory -Force .\build\frontend | Out-Null
Copy-Item .\frontend\dist\frontend\browser\* .\build\frontend -Recurse -Force

# ============================================
# B. DOCKER
# ============================================
# Change the version for every release.
$VERSION = "student-manager-v2"
$ECR = "583164063998.dkr.ecr.us-east-1.amazonaws.com/simple-python-app"

# Build image
docker build --no-cache -t simple-python-app:$VERSION .

# ============================================
# C. ECR AUTH
# ============================================
aws ecr get-login-password --region us-east-1 | docker login --username AWS --password-stdin 583164063998.dkr.ecr.us-east-1.amazonaws.com

# ============================================
# D. TAG + PUSH
# ============================================
docker tag simple-python-app:$VERSION $ECR`:$VERSION
docker push $ECR`:$VERSION

# ============================================
# E. EKS
# ============================================
kubectl set image deployment/simple-python-app simple-python-app=$ECR`:$VERSION

# Check deployment image
kubectl get deployment simple-python-app -o jsonpath="{.spec.template.spec.containers[0].image}"

# Check pod
kubectl get pods -o wide

# Watch rollout
kubectl rollout status deployment/simple-python-app
```

If the new pod is stuck because of `Too many pods`, use the clean replacement procedure in Section 21.

---

# 42. Full demo startup — two independent windows

## WINDOW 1 — APPLICATION ACCESS

```powershell
cd D:\RTB_Agents\APP_EKS

# Expose the EKS app to localhost.
kubectl port-forward deployment/simple-python-app 5000:5000
```

## WINDOW 2 — EUNOIA RCA AGENT

```powershell
cd D:\RTB_Agents\RCA_Agent

# Start automatic RCA monitoring.
python .\main.py
```

## BROWSER

```text
http://127.0.0.1:5000
```

---

# 43. Jury explanation

Use this wording:

> The application is a simple Student Record Manager built with Angular, Python Flask, and a CSV data store. The application runs normally. When a real runtime problem occurs, the error is logged by the application and centralized in CloudWatch through EKS and Fluent Bit. Our Eunoia DevOps agent continuously monitors the new log events. It automatically detects the new error, invokes Agent-Eunoia for Root Cause Analysis, and stores the generated RCA report according to the affected layer — frontend, backend, or database. The next stage is a second remediation agent that proposes a code fix and waits for administrator approval before changing or redeploying the application.

---

# 44. Future Agent 2 flow

```text
RCA Report
    ↓
Agent 2 / Remediation Agent
    ↓
Proposed code change / diff
    ↓
Admin approval
    ↓
Apply change
    ↓
Run tests
    ↓
Build Docker image
    ↓
Push ECR
    ↓
Redeploy EKS
    ↓
Verify health
```

Human approval is required before automatic code modification or redeployment.

---

# 45. Do NOT do these things

```text
DO NOT create a second ECR repository.
DO NOT create another EKS application deployment.
DO NOT create another CloudWatch log group.
DO NOT hard-code a pod name in the RCA agent.
DO NOT manually feed a fake error into Eunoia.
DO NOT use pre-written RCA reports as the trigger.
DO NOT add visible Generate Error buttons to the Student Record Manager.
DO NOT expose API keys or AWS credentials.
DO NOT let Agent 2 modify production code without admin approval.
```

---

# 46. Quick health checklist

Before a jury demo, verify:

```text
[ ] AWS CLI authenticated
[ ] kubectl connected to EKS
[ ] simple-python-app deployment exists
[ ] application pod is 1/1 Running
[ ] correct ECR image is deployed
[ ] port-forward works on localhost:5000
[ ] Student Record Manager loads
[ ] students can be added/viewed/deleted
[ ] RCA Agent starts independently
[ ] RCA Agent reads the correct .env
[ ] CloudWatch application stream is active
[ ] frontend error produces FRONTEND_ERROR
[ ] backend error produces BACKEND_ERROR + traceback
[ ] CSV error produces DATABASE_ERROR + traceback
[ ] Eunoia is invoked automatically
[ ] report appears under the correct reports/ directory
[ ] duplicate reports are not created for the same event
```

---

# 47. Core one-line architecture

```text
Angular → Flask → CSV → Docker → ECR → EKS → Fluent Bit → CloudWatch → RCA Agent → Azure Agent-Eunoia → Layer-specific RCA Report
```
