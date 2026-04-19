void setup() {
  // spustit sériový port
  Serial.begin(300);
}

void loop() {
  // Čtení analogových hodnot (0-1023)
  int raw0 = analogRead(A0);
  int raw1 = analogRead(A1);
  int raw2 = analogRead(A2);

  // Převod na napětí (referenční napětí Nano = 5V)
  float v0 = (raw0 * 5.0) / 1023.0;
  float v1 = (raw1 * 5.0) / 1023.0;
  float v2 = (raw2 * 5.0) / 1023.0;

  // Výpis do konzole
  Serial.print("A0: "); Serial.print(v0, 3); Serial.print(" V  |  ");
  Serial.print("A1: "); Serial.print(v1, 3); Serial.print(" V  |  ");
  Serial.print("A2: "); Serial.print(v2, 3); Serial.println(" V");
}
