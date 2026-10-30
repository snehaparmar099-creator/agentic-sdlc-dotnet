namespace UrlShortener.Tests;

public class CodeGeneratorTests
{
    [Fact]
    public void Code_has_requested_length_and_alphabet()
    {
        var code = CodeGenerator.Generate(9);
        Assert.Equal(9, code.Length);
        Assert.All(code, c => Assert.True(CodeGenerator.Alphabet.Contains(c)));
    }

    [Fact]
    public void Codes_are_not_trivially_repeated()
    {
        var codes = Enumerable.Range(0, 500).Select(_ => CodeGenerator.Generate(7)).ToHashSet();
        Assert.Equal(500, codes.Count);
    }

    [Fact]
    public void Rejects_too_short_length()
    {
        Assert.Throws<ArgumentOutOfRangeException>(() => CodeGenerator.Generate(3));
    }
}
