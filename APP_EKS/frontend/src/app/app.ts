import { CommonModule } from '@angular/common';
import { Component } from '@angular/core';
import { FormsModule } from '@angular/forms';

interface Student {
  id: string;
  name: string;
  age: string;
  grade: string;
}

interface ReviewEvidence {
  file: string;
  line: number;
  text: string;
}

interface CriterionReview {
  criterion: string;
  status: 'met' | 'partially_met' | 'missing';
  keyword_coverage: number;
  evidence: ReviewEvidence[];
  note: string;
}

interface ReviewResult {
  method: string;
  user_story: string | null;
  notice: string;
  summary: { met: number; partially_met: number; missing: number };
  criteria: CriterionReview[];
  documentation_draft: string;
}

@Component({
  selector: 'app-root',
  standalone: true,
  imports: [CommonModule, FormsModule],
  templateUrl: './app.html',
  styleUrl: './app.css'
})
export class App {
  title = 'Student Record Manager';

  students: Student[] = [];

  name = '';
  age: number | null = null;
  grade = 'A';

  searchId = '';

  message = 'Ready';
  summary: { count: number; average_age: number } | null = null;
  reviewTicket = '';
  implementationDiff = '';
  changedFiles: { path: string; content: string }[] = [];
  reviewResult: ReviewResult | null = null;
  reviewInProgress = false;

  ngOnInit() {
    void this.loadStudents();
  }

  async loadStudents() {
    try {
      const response = await fetch('/api/students');
      const data = await response.json();

      if (!response.ok) {
        throw new Error(data.error || 'Unable to load students');
      }

      this.students = data;
      this.message = `${this.students.length} student(s) loaded`;
      this.summary = null;

    } catch (error: any) {
      console.error(error);
      this.message = 'Unable to load student records';
    }
  }

  async addStudent() {
    if (!this.name || !this.age) {
      this.message = 'Please enter name and age';
      return;
    }

    const response = await fetch('/api/students', {
      method: 'POST',
      headers: {
        'Content-Type': 'application/json'
      },
      body: JSON.stringify({
        name: this.name,
        age: this.age,
        grade: this.grade
      })
    });

    const data = await response.json();

    if (response.ok) {
      this.message = 'Student added successfully';
      this.name = '';
      this.age = null;
      this.grade = 'A';

      await this.loadStudents();

    } else {
      this.message = data.error || 'Unable to add student';
    }
  }

  async deleteStudent(id: string) {
    const response = await fetch(`/api/students/${id}`, {
      method: 'DELETE'
    });

    const data = await response.json();

    if (response.ok) {
      this.message = 'Student deleted successfully';
      await this.loadStudents();
    } else {
      this.message = data.error || 'Unable to delete student';
    }
  }

  async searchStudent() {
    if (!this.searchId) {
      this.message = 'Enter a student ID';
      return;
    }

    try {
      const response = await fetch(`/api/students/${this.searchId}`);
      const data = await response.json();

      if (!response.ok) {
        this.message = data.error || 'Student not found';
        return;
      }

      this.message =
        `Student found: ${data.name} (Grade ${data.grade})`;

    } catch (error: any) {
      await this.reportFrontendError(
        error,
        'searchStudent',
        `/api/students/${this.searchId}`
      );
      this.message = 'Unable to complete the operation.';
      console.error(error);
    }
  }

  async loadSummary() {
    const response = await fetch('/api/summary');
    const data = await response.json();

    if (response.ok) {
      this.summary = data;
      this.message = 'Class summary loaded';
    } else {
      this.message = data.error || 'Unable to generate summary';
    }

    async loadSampleReview() {
      try {
        const response = await fetch('/api/reviews/sample');
        const data = await response.json();
        if (!response.ok) {
          throw new Error(data.error || 'Unable to load sample review');
        }
        this.reviewTicket = data.ticket;
        this.implementationDiff = data.diff;
        this.changedFiles = [];
        this.reviewResult = null;
        this.message = 'Sample Jira ticket and diff loaded';
      } catch (error) {
        console.error(error);
        this.message = 'Unable to load the sample Jira review';
      }
    }

    async onChangedFilesSelected(event: Event) {
      const input = event.target as HTMLInputElement;
      const files = Array.from(input.files ?? []);
      try {
        this.changedFiles = await Promise.all(files.map(async file => ({
          path: file.webkitRelativePath || file.name,
          content: await file.text()
        })));
        this.implementationDiff = '';
        this.reviewResult = null;
        this.message = `${this.changedFiles.length} changed file(s) ready`;
      } catch (error) {
        console.error(error);
        this.changedFiles = [];
        this.message = 'Unable to read the selected files';
      }
    }

    async reviewChanges() {
      if (this.reviewInProgress) {
        return;
      }
      this.reviewInProgress = true;
      this.reviewResult = null;
      try {
        const response = await fetch('/api/reviews', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({
            ticket: this.reviewTicket,
            diff: this.implementationDiff,
            changed_files: this.changedFiles
          })
        });
        const data = await response.json();
        if (!response.ok) {
          throw new Error(data.error || 'Unable to review changes');
        }
        this.reviewResult = data;
        this.message = 'Jira change review completed; findings are provisional';
      } catch (error) {
        console.error(error);
        this.message = error instanceof Error
          ? error.message
          : 'Unable to review changes';
      } finally {
        this.reviewInProgress = false;
      }
    }

    statusLabel(status: CriterionReview['status']) {
      return status === 'partially_met'
        ? 'Partially met'
        : status.charAt(0).toUpperCase() + status.slice(1);
    }

    async copyDocumentationDraft() {
      if (!this.reviewResult) {
        return;
      }
      try {
        await navigator.clipboard.writeText(
          this.reviewResult.documentation_draft
        );
        this.message = 'Documentation draft copied';
      } catch (error) {
        console.error(error);
        this.message = 'Unable to copy the documentation draft; select its text to copy';
      }
    }
  }

  private async reportFrontendError(
    error: any,
    context: string,
    endpoint: string
  ) {
    await fetch('/api/error/frontend', {
      method: 'POST',
      headers: {
        'Content-Type': 'application/json'
      },
      body: JSON.stringify({
        message: error?.message || String(error),
        stack: error?.stack || 'No stack trace',
        context,
        endpoint,
        timestamp: new Date().toISOString()
      })
    }).catch(() => {
      // Avoid hiding the original frontend failure.
    });
  }
}
