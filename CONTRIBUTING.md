# Contributing to Monitor App 🚀

Thank you for your interest in contributing to Monitor App! We welcome contributions from developers of all skill levels. This document provides guidelines and information for contributors.

## 📋 Table of Contents

- [Code of Conduct](#code-of-conduct)
- [Getting Started](#getting-started)
- [Development Setup](#development-setup)
- [How to Contribute](#how-to-contribute)
- [Pull Request Process](#pull-request-process)
- [Coding Standards](#coding-standards)
- [Testing Guidelines](#testing-guidelines)
- [Documentation](#documentation)
- [Issue Reporting](#issue-reporting)
- [Community](#community)

## 📜 Code of Conduct

By participating in this project, you agree to abide by our Code of Conduct. We are committed to providing a welcoming and inspiring community for all.

### Our Standards

- **Be respectful** and inclusive in your communication
- **Be collaborative** and help others learn
- **Be patient** with newcomers and different skill levels
- **Focus on constructive feedback** rather than criticism
- **Respect different viewpoints** and experiences

## 🚀 Getting Started

### Prerequisites

- Python 3.10 or higher
- Git
- Poetry (recommended) or pip

### Quick Setup

1. **Fork the repository** on GitHub
2. **Clone your fork** locally:
   ```bash
   git clone https://github.com/YOUR-USERNAME/monitor-app.git
   cd monitor-app
   ```
3. **Install dependencies**:
   ```bash
   poetry install
   ```
4. **Run tests** to ensure everything works:
   ```bash
   poetry run python -m pytest tests/ -v
   ```

## 🛠️ Development Setup

### Using Poetry (Recommended)

```bash
# Install dependencies
poetry install

# Activate virtual environment
poetry shell

# Run the application
poetry run python monitor_app/app.py runserver --debug --csv
```

### Using pip

```bash
# Create virtual environment
python -m venv venv
source venv/bin/activate  # On Windows: venv\Scripts\activate

# Install dependencies
pip install -e .
pip install pytest black

# Run the application
python monitor_app/app.py runserver --debug --csv
```

### Environment Setup

Create test CSV files for development:
```bash
mkdir -p monitor_app/csv
echo "id,name,email" > monitor_app/csv/users.csv
echo "1,Test User,test@example.com" >> monitor_app/csv/users.csv
```

## 🤝 How to Contribute

### Types of Contributions

We welcome various types of contributions:

#### 🐛 Bug Reports
- Use the GitHub issue tracker
- Include detailed steps to reproduce
- Provide system information (OS, Python version)
- Include error messages and stack traces

#### ✨ Feature Requests
- Check existing issues first
- Describe the problem you're trying to solve
- Explain how the feature would benefit users
- Consider implementation complexity

#### 📝 Documentation
- Fix typos and grammatical errors
- Improve existing explanations
- Add missing documentation
- Create tutorials and examples

#### 🔧 Code Contributions
- Bug fixes
- Feature implementations
- Performance improvements
- Code refactoring

#### 🧪 Testing
- Add missing test coverage
- Improve test quality
- Add integration tests
- Performance testing

## 🔄 Pull Request Process

### Before You Start

1. **Check existing issues** to avoid duplicate work
2. **Open an issue** for major changes to discuss first
3. **Fork the repository** and create a feature branch
4. **Follow coding standards** and write tests

### Step-by-Step Process

1. **Create a branch** from `main`:
   ```bash
   git checkout -b feature/your-feature-name
   # or
   git checkout -b fix/bug-description
   ```

2. **Make your changes**:
   - Follow the coding standards
   - Add tests for new functionality
   - Update documentation if needed

3. **Run tests and formatting**:
   ```bash
   # Format code
   poetry run black .
   
   # Run tests
   poetry run python -m pytest tests/ -v
   
   # Run type checking (if mypy is available)
   poetry run mypy monitor_app/ --ignore-missing-imports
   ```

4. **Commit your changes**:
   ```bash
   git add .
   git commit -m "Add feature: brief description of changes"
   ```

5. **Push to your fork**:
   ```bash
   git push origin feature/your-feature-name
   ```

6. **Create a Pull Request**:
   - Use a clear, descriptive title
   - Reference related issues with `#issue-number`
   - Describe what changed and why
   - Include screenshots for UI changes

### Pull Request Template

```markdown
## Description
Brief description of changes made.

## Type of Change
- [ ] Bug fix (non-breaking change which fixes an issue)
- [ ] New feature (non-breaking change which adds functionality)
- [ ] Breaking change (fix or feature that would cause existing functionality to not work as expected)
- [ ] Documentation update

## How Has This Been Tested?
- [ ] Unit tests pass
- [ ] Integration tests pass
- [ ] Manual testing performed

## Related Issues
Closes #(issue number)

## Screenshots (if applicable)
Add screenshots here

## Checklist
- [ ] My code follows the project's coding standards
- [ ] I have performed a self-review of my own code
- [ ] I have commented my code in hard-to-understand areas
- [ ] I have made corresponding changes to the documentation
- [ ] My changes generate no new warnings
- [ ] I have added tests that prove my fix is effective or that my feature works
- [ ] New and existing unit tests pass locally with my changes
```

## 📏 Coding Standards

### Python Code Style

We use **Black** for code formatting and follow **PEP 8** conventions.

#### Formatting
```bash
# Auto-format all code
poetry run black .

# Check formatting without changing files
poetry run black --check .
```

#### Code Quality Guidelines

1. **Use descriptive variable names**:
   ```python
   # Good
   user_email = "user@example.com"
   
   # Bad
   e = "user@example.com"
   ```

2. **Write docstrings for functions and classes**:
   ```python
   def create_user(name: str, email: str) -> dict:
       """Create a new user with the given name and email.
       
       Args:
           name: The user's full name
           email: The user's email address
           
       Returns:
           Dict containing user information
           
       Raises:
           ValueError: If email format is invalid
       """
   ```

3. **Use type hints** (when available):
   ```python
   def get_user_by_id(user_id: int) -> Optional[dict]:
       """Get user by ID."""
   ```

4. **Follow Flask conventions**:
   - Use blueprints for modular code
   - Handle exceptions appropriately
   - Use SQLAlchemy best practices

### Configuration Management

When modifying `monitor_app/config/config.py`:

1. **Add new settings at the end** of relevant sections
2. **Provide sensible defaults** for all settings
3. **Document new settings** in README.md
4. **Update tests** to include new configuration

### API Design Principles

1. **RESTful conventions**:
   - GET for retrieval
   - POST for creation
   - PUT for updates
   - DELETE for removal

2. **Consistent response formats**:
   ```python
   # Success response
   {"success": True, "data": {...}, "message": "Operation completed"}
   
   # Error response
   {"error": "Description of what went wrong"}
   ```

3. **Input validation**:
   - Validate all user inputs
   - Return meaningful error messages
   - Use appropriate HTTP status codes

## 🧪 Testing Guidelines

### Test Structure

Our test suite includes:
- **Unit tests**: Individual functions and methods
- **Integration tests**: API endpoints and database interactions
- **Configuration tests**: Settings validation

### Writing Tests

1. **Test file location**:
   ```
   tests/
   ├── test_api.py        # REST API tests
   ├── test_app.py        # Web application tests
   └── test_config.py     # Configuration tests
   ```

2. **Test naming convention**:
   ```python
   def test_create_user_success(self, client):
       """Test successful user creation."""
   
   def test_create_user_invalid_email(self, client):
       """Test user creation with invalid email."""
   ```

3. **Use descriptive assertions**:
   ```python
   # Good
   assert response.status_code == 200
   assert "success" in response_data
   assert response_data["data"]["name"] == "John Doe"
   
   # Avoid
   assert response.status_code == 200 and "success" in response_data
   ```

### Running Tests

```bash
# Run all tests
poetry run python -m pytest tests/ -v

# Run specific test file
poetry run python -m pytest tests/test_api.py -v

# Run specific test class
poetry run python -m pytest tests/test_api.py::TestUsersAPI -v

# Run with coverage
poetry run python -m pytest tests/ --cov=monitor_app --cov-report=html
```

### Test Requirements

- **All new features** must include tests
- **Bug fixes** should include regression tests
- **Tests must pass** before PR approval
- **Maintain high test coverage** (aim for >90%)

## 📖 Documentation

### Types of Documentation

1. **Code documentation**: Docstrings and comments
2. **API documentation**: Automatic Swagger generation
3. **User documentation**: README.md and tutorials
4. **Developer documentation**: This file and technical guides

### Documentation Standards

1. **README.md updates**:
   - Update feature lists for new functionality
   - Add configuration examples for new settings
   - Include usage examples

2. **API documentation**:
   - Add docstrings to Flask routes for Swagger
   - Include parameter descriptions
   - Provide response examples

3. **Code comments**:
   - Explain complex logic
   - Document why, not just what
   - Keep comments up-to-date

## 🐛 Issue Reporting

### Before Creating an Issue

1. **Search existing issues** to avoid duplicates
2. **Check the documentation** for solutions
3. **Try the latest version** to see if it's already fixed

### Creating a Good Issue

#### Bug Report Template

```markdown
## Bug Description
A clear and concise description of the bug.

## Steps to Reproduce
1. Go to '...'
2. Click on '...'
3. Scroll down to '...'
4. See error

## Expected Behavior
What you expected to happen.

## Actual Behavior
What actually happened.

## Environment
- OS: [e.g. Ubuntu 20.04]
- Python Version: [e.g. 3.10.5]
- Monitor App Version: [e.g. 0.1.8]

## Additional Context
Add any other context, screenshots, or error logs.
```

#### Feature Request Template

```markdown
## Feature Description
A clear and concise description of the feature.

## Problem Statement
What problem would this feature solve?

## Proposed Solution
Describe your preferred solution.

## Alternatives Considered
Describe alternative solutions you've considered.

## Additional Context
Add any other context or screenshots.
```

## 👥 Community

### Getting Help

- **GitHub Issues**: For bugs and feature requests
- **GitHub Discussions**: For questions and general discussion
- **Documentation**: Check README.md and inline documentation

### Communication Guidelines

1. **Be respectful** and professional
2. **Provide context** when asking for help
3. **Share knowledge** by helping others
4. **Follow up** on your issues and PRs

### Recognition

Contributors are recognized in several ways:
- Listed in project contributors
- Mentioned in release notes for significant contributions
- Special recognition for ongoing contributions

## 🎯 Areas Where We Need Help

### High Priority
- **Documentation improvements** (tutorials, examples)
- **Bug fixes** and stability improvements
- **Performance optimizations**
- **Test coverage expansion**

### Medium Priority
- **Feature enhancements** (data visualization, export features)
- **UI/UX improvements**
- **Mobile responsiveness**
- **Accessibility improvements**

### Future Goals
- **Plugin system** architecture
- **Multi-language support**
- **Advanced authentication** systems
- **Cloud deployment** guides

## 📞 Contact

- **Project Maintainer**: [@hardwork9047](https://github.com/hardwork9047)
- **Issues**: [GitHub Issues](https://github.com/hardwork9047/monitor-app/issues)
- **Discussions**: [GitHub Discussions](https://github.com/hardwork9047/monitor-app/discussions)

---

## 🙏 Thank You

Thank you for taking the time to contribute to Monitor App! Every contribution, no matter how small, helps make this project better for everyone.

**Happy coding!** 🚀