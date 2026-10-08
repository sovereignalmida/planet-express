from scripts import envfile_exec


def test_dollar_signs_in_values_stay_literal(tmp_path):
    env = tmp_path / "dash.env"
    env.write_text('# comment\nPE_X="scrypt:32768:8:1$salt$hash"\nPLAIN=abc\n')
    assert envfile_exec.load(str(env)) == {"PE_X": "scrypt:32768:8:1$salt$hash", "PLAIN": "abc"}
