import { CommonModule } from '@angular/common';
import { Component } from '@angular/core';
import { FormsModule } from '@angular/forms';

interface Student {
  id: string;
  name: string;
  age: string;
  grade: string;
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

      /*
       * Intentional frontend defect for RCA demonstration.
       *
       * When the backend says the student does not exist,
       * the code incorrectly assumes "student" exists.
       */
      if (!response.ok) {
        const student = data.student;
        const studentName = student.name;

        this.message = `Student: ${studentName}`;
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
