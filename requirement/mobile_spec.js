```javascript
describe('Mobile Voice E2E', () => {
  it('should handle voice mode', () => {
    cy.visit('/login');
    cy.get('#username').type('user123');
    cy.get('#password').type('Test123!');
    cy.get('#submit_login').click();

    cy.get('#main_menu a[href="/chat"]').click();
    cy.get('#voice_mode_button').click();
    // Mock voice input
    cy.get('#chat_input').type('Hello, need help');
    cy.get('#send_button').click();
    cy.get('.chat_history').should('contain', 'How can I assist you?');
  });
});
```